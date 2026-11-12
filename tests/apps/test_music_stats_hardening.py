#!/usr/bin/env python3
"""
Hardening tests for the persisted platform breaker and aggregate accounting.

Everything here is deterministic: the clock is replaced through
``aggregate._now``, "another process" is simulated by writing the stats file
between load and save, and I/O failures are injected with monkeypatch.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from novel_downloader.apps.music import aggregate
from novel_downloader.apps.music.providers.base import Track

STATS_NAME = "search_stats.json"


def _track(name: str, provider: str) -> Track:
    return Track(provider=provider, id=f"{provider}-{name}", name=name, artists="A")


@pytest.fixture()
def stats_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect the stats file to an isolated dir with a fresh in-memory cache."""
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    monkeypatch.setattr(aggregate, "_STATS", None)
    monkeypatch.setattr(aggregate, "_STATS_WRITABLE", True)
    monkeypatch.setattr(aggregate, "_STATS_SIG", None)
    return tmp_path


def _write_stats(home: Any, platforms: Any) -> Any:
    path = home / STATS_NAME
    path.write_text(
        json.dumps({"version": 1, "platforms": platforms}), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# The stats file must never take the search down
# ---------------------------------------------------------------------------


def test_corrupt_stats_file_fails_open_and_is_repaired(stats_home: Any) -> None:
    path = stats_home / STATS_NAME
    path.write_text("{definitely not json", encoding="utf-8")

    assert aggregate._load_stats() == {}
    assert aggregate._breaker_open("dead") is False
    assert aggregate.failed_platforms() == []

    aggregate._record_failure("dead", 12)
    aggregate._save_stats()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["platforms"]["dead"]["ok"] is False
    assert payload["platforms"]["dead"]["last_ms"] == 12


def test_one_bad_entry_does_not_discard_the_whole_file(stats_home: Any) -> None:
    """Wrong field types are sanitised per entry, not turned into a global reset."""
    _write_stats(
        stats_home,
        {
            "good": {"ok": True, "last_ms": 5, "failed_until": 0},
            "bad": {"ok": "yes", "last_ms": "abc", "failed_until": [1]},
            "huge": {"ok": True, "last_ms": 3, "failed_until": 1e18},
            "streak": {"ok": False, "last_ms": 1, "empty_streak": "many"},
            "not-a-dict": 3,
        },
    )

    data = aggregate._load_stats()

    assert set(data) == {"good", "bad", "huge", "streak"}
    assert data["good"] == {"ok": True, "last_ms": 5, "failed_until": 0.0}
    assert data["bad"] == {"ok": False, "last_ms": 0, "failed_until": 0.0}
    # 1e18 is far beyond any legitimate TTL: it must not become a permanent ban.
    assert data["huge"]["failed_until"] == 0.0
    assert "empty_streak" not in data["streak"]


def test_non_finite_deadlines_never_lock_a_platform_forever(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NaN compares False with everything, so the old breaker never expired."""
    path = stats_home / STATS_NAME
    # json.loads accepts the bare NaN/Infinity tokens Python's json emits.
    path.write_text(
        '{"version":1,"platforms":{'
        '"nan":{"ok":false,"failed_until":NaN},'
        '"inf":{"ok":false,"failed_until":Infinity},'
        '"neg":{"ok":false,"failed_until":-1e9}}}',
        encoding="utf-8",
    )

    assert aggregate._load_stats()["nan"]["failed_until"] == 0.0
    assert aggregate._load_stats()["inf"]["failed_until"] == 0.0
    assert aggregate._breaker_open("nan") is False
    assert aggregate._breaker_open("inf") is False
    assert aggregate._breaker_open("neg") is False
    assert aggregate.failed_platforms() == []

    # Even if such a value reaches the in-memory map directly.
    monkeypatch.setattr(
        aggregate,
        "_STATS",
        {
            "x": {"ok": False, "last_ms": 1, "failed_until": float("nan")},
            "y": {"ok": False, "last_ms": 1, "failed_until": float("inf")},
        },
    )
    assert aggregate._breaker_open("x") is False
    assert aggregate._breaker_open("y") is False
    assert aggregate.failed_platforms() == []


def test_oversized_stats_file_is_ignored_and_replaced(stats_home: Any) -> None:
    """A huge file must not be parsed into memory just to read 3 fields."""
    path = stats_home / STATS_NAME
    filler = b'"x":{},' * (aggregate._STATS_MAX_BYTES // 6 + 1000)
    path.write_bytes(b'{"platforms":{' + filler + b'"y":{}}}')
    assert path.stat().st_size > aggregate._STATS_MAX_BYTES

    assert aggregate._load_stats() == {}
    assert aggregate._breaker_open("x") is False

    aggregate._record_failure("y", 5)
    aggregate._save_stats()
    assert path.stat().st_size < aggregate._STATS_MAX_BYTES


def test_stats_entry_count_is_bounded(stats_home: Any) -> None:
    _write_stats(
        stats_home,
        {f"p{index}": {"ok": True, "last_ms": 1} for index in range(500)},
    )

    assert len(aggregate._load_stats()) == aggregate._STATS_MAX_ENTRIES


def test_unreadable_stats_file_is_never_overwritten(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read error must not replace a possibly-good file with empty stats."""
    path = _write_stats(
        stats_home,
        {"other": {"ok": False, "last_ms": 1, "failed_until": 0}},
    )
    original = path.read_bytes()
    real_reader = aggregate._read_stats_file

    def unreadable(_path: Any) -> Any:
        return {}, False, None

    monkeypatch.setattr(aggregate, "_read_stats_file", unreadable)

    assert aggregate._load_stats() == {}
    aggregate._record_failure("mine", 5)
    aggregate._save_stats()

    assert path.read_bytes() == original  # untouched, and no temp left behind
    assert list(stats_home.glob("*.tmp")) == []

    # Once the file is readable again, saves resume and merge both sides.
    monkeypatch.setattr(aggregate, "_read_stats_file", real_reader)
    aggregate._save_stats()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["platforms"]) == {"mine", "other"}


def test_save_merges_entries_written_by_another_process(stats_home: Any) -> None:
    """Last-writer-wins must not silently drop another process's platforms."""
    aggregate._load_stats()  # file does not exist yet
    foreign = _write_stats(
        stats_home,
        {"foreign": {"ok": False, "last_ms": 7, "failed_until": 0}},
    )

    aggregate._record_failure("mine", 3)
    aggregate._save_stats()

    payload = json.loads(foreign.read_text(encoding="utf-8"))
    assert set(payload["platforms"]) == {"mine", "foreign"}
    assert payload["platforms"]["foreign"]["last_ms"] == 7


def test_failed_write_keeps_the_previous_file(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_stats(
        stats_home, {"old": {"ok": True, "last_ms": 1, "failed_until": 0}}
    )
    original = path.read_bytes()
    aggregate._load_stats()
    aggregate._record_failure("new", 4)

    def broken_dump(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(aggregate.json, "dump", broken_dump)

    aggregate._save_stats()  # must not raise

    assert path.read_bytes() == original
    assert list(stats_home.glob("*.tmp")) == []


def test_unavailable_music_dir_never_breaks_the_breaker(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise OSError("cannot create data dir")

    monkeypatch.setattr(aggregate, "_stats_file", broken)

    assert aggregate._load_stats() == {}
    assert aggregate._breaker_open("x") is False
    aggregate._record_failure("x", 1)
    aggregate._save_stats()  # must not raise
    assert aggregate._breaker_open("x") is True


def test_clear_removes_entries_written_after_the_load(stats_home: Any) -> None:
    """An explicit clear must erase the file, not merge foreign entries back in."""
    aggregate._load_stats()
    path = _write_stats(
        stats_home, {"foreign": {"ok": False, "last_ms": 1, "failed_until": 0}}
    )

    aggregate.clear_failure_cache()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["platforms"] == {}


# ---------------------------------------------------------------------------
# Breaker TTL / slow-empty streak races
# ---------------------------------------------------------------------------


def test_expired_breaker_resets_the_slow_empty_streak(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After SLOW_EMPTY_TTL the platform gets a fresh two-strike window."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(aggregate, "_now", lambda: clock["now"])

    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    assert aggregate._breaker_open("slow") is False  # streak 1

    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    assert aggregate._breaker_open("slow") is True  # streak 2 -> open

    clock["now"] += aggregate.SLOW_EMPTY_TTL + 1
    assert aggregate._breaker_open("slow") is False
    assert not aggregate._load_stats()["slow"].get("empty_streak")

    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    assert aggregate._breaker_open("slow") is False  # one strike, not a re-trip


def test_hard_failure_resets_the_slow_empty_streak(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(aggregate, "_now", lambda: clock["now"])

    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS + 1, 0)
    aggregate._record_failure("p", 10)
    assert not aggregate._load_stats()["p"].get("empty_streak")

    clock["now"] += aggregate.FAILURE_TTL + 1
    assert aggregate._breaker_open("p") is False

    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS + 1, 0)
    assert aggregate._breaker_open("p") is False


def test_fast_or_non_empty_results_clear_the_streak(stats_home: Any) -> None:
    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS + 1, 0)
    aggregate._record_success("p", 5, 0)  # fast empty resets
    assert not aggregate._load_stats()["p"].get("empty_streak")

    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS + 1, 3)  # slow, non-empty
    assert not aggregate._load_stats()["p"].get("empty_streak")


# ---------------------------------------------------------------------------
# Aggregate search accounting
# ---------------------------------------------------------------------------


def test_deadline_only_records_platforms_that_actually_started(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform still queued at the deadline had no chance to answer.

    It used to be reported (and persisted) as a timeout, blacklisting an
    entire unsearched tail for ``FAILURE_TTL``.
    """
    started = threading.Event()
    release = threading.Event()

    class Stuck:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            started.set()
            release.wait(5)
            return [], 0

    monkeypatch.setattr(
        aggregate, "available", lambda: [("a", "A"), ("b", "B"), ("c", "C")]
    )
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Stuck())

    holder: list[aggregate.AggregateResult] = []
    thread = threading.Thread(
        target=lambda: holder.append(aggregate.search_all("x", workers=1, timeout=0.2))
    )
    thread.start()
    try:
        assert started.wait(5)
        thread.join(10)
    finally:
        release.set()

    assert holder, "search_all did not return"
    result = holder[0]
    platforms = {item.platform: item for item in result.platforms}
    assert list(platforms) == ["a"]
    assert platforms["a"].ok is False
    assert "超时" in platforms["a"].error

    assert aggregate.failed_platforms() == ["a"]
    assert aggregate._breaker_open("b") is False
    assert aggregate._breaker_open("c") is False


def test_search_persists_every_reported_platform(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final save runs after the last outcome's stats update."""
    monkeypatch.setattr(aggregate, "available", lambda: [("good", "好"), ("bad", "坏")])

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "good")], 1

    def create(key: str, **kwargs: Any) -> Any:
        if key == "bad":
            raise RuntimeError("boom")
        return Good()

    monkeypatch.setattr(aggregate, "create", create)

    result = aggregate.search_all("x", timeout=3.0)

    payload = json.loads((stats_home / STATS_NAME).read_text(encoding="utf-8"))
    for outcome in result.platforms:
        entry = payload["platforms"][outcome.platform]
        assert entry["ok"] is outcome.ok


def test_progressive_updates_end_at_the_returned_result(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No queued callback may deliver a state older than the return value."""
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A"), ("b", "B")])
    monkeypatch.setattr(
        aggregate,
        "create",
        lambda key, **kw: type(
            "P",
            (),
            {
                "search": lambda self, keyword, limit=30, offset=0: (
                    [_track(f"{key}-{keyword}", key)],
                    1,
                )
            },
        )(),
    )

    snapshots: list[aggregate.AggregateResult] = []
    result = aggregate.search_all("x", on_update=snapshots.append, timeout=3.0)

    assert snapshots
    last = snapshots[-1]
    assert [item.platform for item in last.platforms] == [
        item.platform for item in result.platforms
    ]
    assert [track.name for track in last.tracks] == [
        track.name for track in result.tracks
    ]


def test_a_broken_platform_exception_still_yields_a_readable_error(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider exception whose ``__str__`` raises must not crash the merge."""

    class BadError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no string for you")

    class Broken:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            raise BadError()

    monkeypatch.setattr(aggregate, "available", lambda: [("broken", "坏")])
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Broken())

    result = aggregate.search_all("x", timeout=1.0)

    assert result.platforms[0].ok is False
    assert result.platforms[0].error == "BadError"


def test_a_malformed_provider_row_only_drops_that_row(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dict (or any non-Track) in one platform's results must not kill the merge."""

    class Mixed:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [{"name": "not a track"}, _track("real", "mixed")], 2

    monkeypatch.setattr(aggregate, "available", lambda: [("mixed", "混合")])
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Mixed())

    result = aggregate.search_all("x", timeout=1.0)

    assert [track.name for track in result.tracks] == ["real"]
    assert result.platforms[0].ok is True
    assert result.platforms[0].count == 1


def test_cancelled_child_process_does_not_touch_the_breaker(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminated worker never reported an outcome: it must not be a failure."""
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A"), ("b", "B")])

    class NoPayload:
        def poll(self, deadline: float) -> bool:
            return False

        def close(self) -> None:
            pass

    class Process:
        daemon = True

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            pass

        def terminate(self) -> None:
            pass

    class Context:
        def Pipe(self, duplex: bool = False) -> Any:
            return NoPayload(), NoPayload()

        def Process(self, **kwargs: Any) -> Process:
            return Process()

    monkeypatch.setattr(
        aggregate.multiprocessing, "get_context", lambda method: Context()
    )

    result = aggregate.search_all_isolated("x", timeout=0.1)

    assert {item.platform for item in result.platforms} == {"a", "b"}
    assert all(item.ok is False for item in result.platforms)
    assert aggregate.failed_platforms() == []


def test_time_budget_is_clamped_for_absurd_values(
    stats_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero/negative timeout still needs a measurable window (0.1s)."""
    monkeypatch.setattr(aggregate, "available", lambda: [("slow", "慢")])

    class Slow:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            time.sleep(0.3)
            return [], 0

    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Slow())

    result = aggregate.search_all("x", timeout=-5)

    assert result.platforms[0].ok is False
    assert aggregate._load_stats()["slow"]["last_ms"] == 100
