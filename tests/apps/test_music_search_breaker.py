#!/usr/bin/env python3
"""
Breaker and "slow and empty" accounting for aggregate music search.

These tests pin the semantics of ``aggregate.search_stats.json`` and of the
``music health`` probe, all offline:

* a platform that fails is skipped (without a request) for ``FAILURE_TTL`` and
  is searched again once the ban expires;
* a success (any answer) resets the ban; there is no failure *counter*, the
  breaker is a deadline, so one failure is enough to skip. A slow *empty*
  answer is a success for connectivity but the first slow-empty strike;
* a platform that twice takes ``SLOW_EMPTY_MS`` and returns nothing is skipped
  for ``SLOW_EMPTY_TTL``; a fast empty or a slow non-empty result resets it;
* concurrent searches / recorders never mix one platform's state into another
  and never corrupt the file;
* a corrupt or unwritable stats file degrades to in-memory state, it never
  fails a search;
* ``music health``'s ``failed`` count and the breaker agree, because a probe is
  merged into the same stats;
* during a ban, ``platform="all"`` still returns the other platforms and lists
  the skipped one in ``platforms[]`` with a fixed reason;
* clearing the cache (explicit call, or the automatic "everything is banned"
  retry) makes the next search probe the platform again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import music as music_cli
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.music import aggregate, providers
from novel_downloader.apps.music.providers import health as health_mod
from novel_downloader.apps.music.providers.base import Track

STATS_NAME = "search_stats.json"


def _track(name: str, provider: str) -> Track:
    return Track(provider=provider, id=f"{provider}-{name}", name=name, artists="A")


class _NoopTimer:
    def cancel(self) -> None:
        pass


class _JumpClock:
    """A monotonic clock that jumps ``step`` seconds on every reading.

    With a generous search budget, one instant provider search therefore
    *measures* as longer than ``SLOW_EMPTY_MS`` without any real sleep, while
    the total budget stays far from expiring. ``wait`` still blocks on the real
    event so every worker has finished before the search returns.
    """

    def __init__(self, step: float = 2.5) -> None:
        self.step = step
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += self.step
        return self._now

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(5.0)

    def schedule(self, delay: float, callback: Any) -> _NoopTimer:
        return _NoopTimer()


@pytest.fixture()
def stats_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the stats file and start every test from an empty cache."""
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    monkeypatch.setattr(aggregate, "_STATS", {})
    monkeypatch.setattr(aggregate, "_STATS_WRITABLE", True)
    monkeypatch.setattr(aggregate, "_STATS_SIG", None)
    return tmp_path


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Controllable wall clock for the breaker deadlines."""
    state = {"now": 1000.0}
    monkeypatch.setattr(aggregate, "_now", lambda: state["now"])
    return state


def _providers(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> list[str]:
    """Register ``keys`` and return the list of platforms that were created."""
    calls: list[str] = []
    monkeypatch.setattr(aggregate, "available", lambda: [(key, key) for key in keys])
    return calls


# ---------------------------------------------------------------------------
# Failure breaker lifecycle
# ---------------------------------------------------------------------------


def test_failure_opens_the_breaker_then_recovers_after_the_ttl(
    stats_home: Path, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _providers(monkeypatch, ["good", "dead"])
    state = {"dead_fails": True}

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "good")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        if key == "dead" and state["dead_fails"]:
            raise RuntimeError("连接失败")
        return Good()

    monkeypatch.setattr(aggregate, "create", create)

    first = aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["good", "dead"]  # ordered: priority/registry order
    assert {item.platform for item in first.platforms} == {"good", "dead"}
    dead = next(item for item in first.platforms if item.platform == "dead")
    assert dead.ok is False and "连接失败" in dead.error
    assert aggregate._breaker_open("dead") is True

    # Inside the window the platform is not even created: no repeated request.
    calls.clear()
    second = aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["good"]
    skipped = next(item for item in second.platforms if item.platform == "dead")
    assert skipped.ok is False
    assert skipped.error == aggregate.SKIPPED_BREAKER_ERROR
    assert [track.name for track in second.tracks] == ["晴天"]

    # After FAILURE_TTL the platform is probed again. It is still dead here, so
    # the retry re-bans it -- the point is that the skip expired.
    clock["now"] += aggregate.FAILURE_TTL + 1
    calls.clear()
    third = aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["good", "dead"]
    assert next(item for item in third.platforms if item.platform == "dead").ok is False
    assert aggregate._breaker_open("dead") is True

    # A recovered platform clears its ban on the next probe after the TTL.
    state["dead_fails"] = False
    clock["now"] += aggregate.FAILURE_TTL + 1
    calls.clear()
    fourth = aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["good", "dead"]
    assert next(item for item in fourth.platforms if item.platform == "dead").ok is True
    assert aggregate._breaker_open("dead") is False


def test_one_success_clears_a_failure_ban_but_a_slow_empty_starts_a_streak(
    stats_home: Path, clock: dict[str, float]
) -> None:
    """There is no failure counter: a ban is a deadline, and success resets it.

    "Success" here means a platform that answered: a *slow empty* answer is a
    success for connectivity but the first strike of the slow-empty streak.
    """
    aggregate._record_failure("p", 50)
    aggregate._record_success("p", 20, 3)
    entry = aggregate._load_stats()["p"]
    assert entry["ok"] is True
    assert entry["failed_until"] == 0.0
    assert entry.get("empty_streak", 0) == 0
    assert aggregate._breaker_open("p") is False

    # One slow empty starts a streak, but only the second one bans.
    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS, 0)
    assert aggregate._load_stats()["p"]["empty_streak"] == 1
    assert aggregate._breaker_open("p") is False
    aggregate._record_success("p", aggregate.SLOW_EMPTY_MS + 500, 0)
    assert aggregate._breaker_open("p") is True

    # A successful non-empty result clears the ban immediately.
    aggregate._record_success("p", 20, 3)
    assert aggregate._breaker_open("p") is False
    assert aggregate._load_stats()["p"].get("empty_streak", 0) == 0


def test_explicit_platform_search_success_resets_the_breaker(
    stats_home: Path, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual retry is how a user un-bans a platform that came back."""
    calls = _providers(monkeypatch, ["flaky"])
    state = {"fail": True}

    class Provider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            if state["fail"]:
                raise RuntimeError("down")
            return [_track(keyword, "flaky")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return Provider()

    monkeypatch.setattr(aggregate, "create", create)

    aggregate.search_all("晴天", timeout=3.0)
    assert aggregate._breaker_open("flaky") is True

    state["fail"] = False
    # Explicit selection never skips, so this is the manual retry.
    aggregate.search_all("晴天", platforms=["flaky"], timeout=3.0)
    assert aggregate._breaker_open("flaky") is False

    calls.clear()
    aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["flaky"]


# ---------------------------------------------------------------------------
# "Slow and empty"
# ---------------------------------------------------------------------------


def test_slow_empty_twice_bans_for_the_slow_ttl(
    stats_home: Path, clock: dict[str, float]
) -> None:
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS - 1, 0)
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS - 1, 0)
    assert aggregate._load_stats()["slow"].get("empty_streak", 0) == 0

    # Slow but with rows is a useful platform; fast but empty is just empty.
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 4)
    aggregate._record_success("slow", 5, 0)
    assert aggregate._load_stats()["slow"].get("empty_streak", 0) == 0

    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    assert aggregate._breaker_open("slow") is True
    until = aggregate._load_stats()["slow"]["failed_until"]
    assert until == pytest.approx(clock["now"] + aggregate.SLOW_EMPTY_TTL)

    clock["now"] += aggregate.SLOW_EMPTY_TTL + 1
    assert aggregate._breaker_open("slow") is False
    # The expired ban also resets the streak: one more observation, not a trip.
    assert aggregate._load_stats()["slow"].get("empty_streak", 0) == 0


def test_slow_empty_observed_by_a_real_search_skips_the_next_one(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two measured slow/empty searches are enough; the third is skipped."""
    calls = _providers(monkeypatch, ["slow", "ok"])
    monkeypatch.setattr(aggregate, "_clock", _JumpClock())

    class Empty:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [], 0

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "ok")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return Empty() if key == "slow" else Good()

    monkeypatch.setattr(aggregate, "create", create)

    for _ in range(2):
        result = aggregate.search_all("晴天", timeout=3.0)
        slow = next(item for item in result.platforms if item.platform == "slow")
        assert slow.ok is True
        assert slow.count == 0

    entry = aggregate._load_stats()["slow"]
    assert entry["empty_streak"] == aggregate.SLOW_EMPTY_STREAK
    assert entry["last_ms"] >= aggregate.SLOW_EMPTY_MS

    # With another platform still healthy, only that one is requested. (When
    # *every* platform is banned the search clears the cache and retries once;
    # that is covered by its own test below.)
    calls.clear()
    skipped = aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["ok"]
    assert [item.platform for item in skipped.platforms] == ["ok", "slow"]
    assert skipped.platforms[1].error == aggregate.SKIPPED_BREAKER_ERROR
    assert [track.name for track in skipped.tracks] == ["晴天"]


def test_fast_empty_searches_do_not_accumulate_a_slow_streak(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(monkeypatch, ["fast"])

    class Empty:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [], 0

    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Empty())

    for _ in range(3):
        assert aggregate.search_all("晴天", timeout=3.0).platforms[0].ok is True

    assert aggregate._load_stats()["fast"].get("empty_streak", 0) == 0
    assert aggregate._breaker_open("fast") is False


# ---------------------------------------------------------------------------
# Isolation between platforms and between searches
# ---------------------------------------------------------------------------


def test_one_platforms_state_never_leaks_into_another(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _providers(monkeypatch, ["ok", "bad", "slow"])

    class Ok:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "ok")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        if key == "bad":
            raise RuntimeError("boom")
        return Ok()

    monkeypatch.setattr(aggregate, "create", create)

    aggregate.search_all("晴天", timeout=3.0)
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)
    aggregate._record_success("slow", aggregate.SLOW_EMPTY_MS + 1, 0)

    stats = aggregate._load_stats()
    assert stats["ok"]["ok"] is True
    assert stats["ok"]["failed_until"] == 0.0
    assert stats["bad"]["ok"] is False
    assert stats["bad"]["failed_until"] > 0
    assert stats["slow"]["ok"] is True
    assert stats["slow"]["empty_streak"] == 2
    assert set(stats) == {"ok", "bad", "slow"}


def test_concurrent_recorders_keep_entries_separate_and_the_file_valid(
    stats_home: Path,
) -> None:
    """Many threads mutating and saving at once must not cross or corrupt."""
    keys = [f"p{index}" for index in range(8)]
    errors: list[BaseException] = []

    def record(key: str) -> None:
        try:
            for index in range(40):
                if index % 2:
                    aggregate._record_success(key, 5, 1)
                else:
                    aggregate._record_failure(key, 7)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    def save() -> None:
        try:
            for _ in range(20):
                aggregate._save_stats()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(key,)) for key in keys]
    threads += [threading.Thread(target=save) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert errors == []
    aggregate._save_stats()
    payload = json.loads((stats_home / STATS_NAME).read_text(encoding="utf-8"))
    assert set(payload["platforms"]) == set(keys)
    for key in keys:
        entry = payload["platforms"][key]
        # Last operation in every thread is a success (index 39 is odd).
        assert entry["ok"] is True
        assert entry["last_ms"] == 5
        assert entry["failed_until"] == 0.0


def test_concurrent_searches_do_not_mix_their_platforms(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping searches (separate threads) keep their own records."""
    calls = _providers(monkeypatch, ["a", "b"])

    class Ok:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "a")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        if key == "a":
            raise RuntimeError("a is down")
        return Ok()

    monkeypatch.setattr(aggregate, "create", create)

    async def run() -> None:
        await asyncio.gather(
            asyncio.to_thread(aggregate.search_all, "x", ["a"], 5, timeout=3.0),
            asyncio.to_thread(aggregate.search_all, "y", ["b"], 5, timeout=3.0),
        )

    asyncio.run(run())

    stats = aggregate._load_stats()
    assert stats["a"]["ok"] is False
    assert stats["b"]["ok"] is True
    assert stats["b"]["failed_until"] == 0.0
    assert set(stats) == {"a", "b"}


# ---------------------------------------------------------------------------
# Degraded stats storage
# ---------------------------------------------------------------------------


def test_corrupt_stats_file_does_not_break_a_search_and_is_repaired(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = stats_home / STATS_NAME
    path.write_text("{not json at all", encoding="utf-8")
    _providers(monkeypatch, ["ok", "bad"])

    class Ok:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "ok")], 1

    def create(key: str, **kwargs: Any) -> Any:
        if key == "bad":
            raise RuntimeError("boom")
        return Ok()

    monkeypatch.setattr(aggregate, "create", create)

    result = aggregate.search_all("晴天", timeout=3.0)

    assert [track.name for track in result.tracks] == ["晴天"]
    assert aggregate._breaker_open("bad") is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["platforms"]) == {"ok", "bad"}


def test_stats_path_that_is_a_directory_degrades_to_memory(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable/unwritable location must not fail or lose the breaker."""
    (stats_home / STATS_NAME).mkdir()
    _providers(monkeypatch, ["ok", "bad"])

    class Ok:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "ok")], 1

    def create(key: str, **kwargs: Any) -> Any:
        if key == "bad":
            raise RuntimeError("boom")
        return Ok()

    monkeypatch.setattr(aggregate, "create", create)

    result = aggregate.search_all("晴天", timeout=3.0)

    assert result.tracks
    # In-memory state still works for the rest of the process.
    assert aggregate._breaker_open("bad") is True
    assert aggregate.failed_platforms() == ["bad"]


def test_stats_file_that_cannot_be_written_keeps_the_search_working(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(monkeypatch, ["ok"])

    class Ok:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "ok")], 1

    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Ok())

    def broken_replace(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(aggregate.os, "replace", broken_replace)

    result = aggregate.search_all("晴天", timeout=3.0)

    assert [track.name for track in result.tracks] == ["晴天"]
    assert list(stats_home.glob("*.tmp")) == []
    assert aggregate._load_stats()["ok"]["ok"] is True


# ---------------------------------------------------------------------------
# music health <-> breaker
# ---------------------------------------------------------------------------


class _HealthEngine:
    """Minimal engine for ``novel-cli music health`` using the real probe."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def labels(self) -> list[tuple[str, str]]:
        return [(key, key.upper()) for key in self._keys]

    def platform_keys(self) -> list[str]:
        return sorted(self._keys)

    def health_check(self, key: str, keyword: str = "test") -> dict[str, Any]:
        return health_mod.health_check(key, keyword)


def _run_health(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> None:
    """Run ``music health --json``.

    The command exits ``1`` whenever a platform failed, so the exit is part of
    the contract and is asserted here instead of leaking into callers.
    """
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    MusicCmd.register(subparsers)
    engine = _HealthEngine(keys)
    monkeypatch.setattr(music_cli, "_require_engine", lambda: engine)
    args = parser.parse_args(["music", "health", "--json"])
    try:
        args.func(args)
    except SystemExit as exc:
        assert exc.code in (0, 1)


def test_health_failed_count_matches_the_breaker_and_clears_on_success(
    stats_home: Path,
    clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``music health`` is a real probe, so it must agree with the breaker.

    It used to report a platform healthy while aggregate search still skipped
    it for up to ten minutes, and a failed probe left no trace at all.
    """
    state = {"qq_fail": True}

    class Provider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "netease")], 1

    def create(key: str, **kwargs: Any) -> Any:
        if key == "qq" and state["qq_fail"]:
            raise RuntimeError("qq down")
        return Provider()

    monkeypatch.setattr(providers, "create", create)

    # A ban recorded by an earlier search is cleared by a successful probe.
    aggregate._record_failure("netease", 10)
    assert aggregate._breaker_open("netease") is True

    _run_health(monkeypatch, ["netease", "qq"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1
    assert [row["platform"] for row in payload["platforms"]] == ["netease", "qq"]
    assert [row["ok"] for row in payload["platforms"]] == [True, False]
    # Same view as the breaker: exactly the platform health called unhealthy.
    assert aggregate.failed_platforms() == ["qq"]
    assert aggregate._breaker_open("netease") is False

    state["qq_fail"] = False
    _run_health(monkeypatch, ["qq"])
    assert json.loads(capsys.readouterr().out)["failed"] == 0
    assert aggregate.failed_platforms() == []
    assert aggregate._breaker_open("qq") is False


def test_health_probe_does_not_invent_stats_for_unknown_platforms(
    stats_home: Path,
) -> None:
    result = health_mod.health_check("definitely-not-a-platform", record_stats=True)

    assert result["ok"] is False
    assert "未知平台" in result["error"]
    # Nothing would ever search that key: no phantom breaker entry.
    assert aggregate._load_stats() == {}
    assert aggregate.failed_platforms() == []


def test_health_probe_can_leave_the_stats_untouched(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        providers,
        "create",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )

    result = health_mod.health_check("netease", record_stats=False)

    assert result["ok"] is False
    assert aggregate._load_stats() == {}


# ---------------------------------------------------------------------------
# Reset / cleanup
# ---------------------------------------------------------------------------


def test_clear_failure_cache_makes_the_next_search_probe_again(
    stats_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _providers(monkeypatch, ["p", "other"])

    class Provider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "x")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return Provider()

    monkeypatch.setattr(aggregate, "create", create)

    aggregate._record_failure("p", 10)
    assert aggregate._breaker_open("p") is True

    # Before the clear, only the healthy platform runs.
    calls.clear()
    aggregate.search_all("晴天", timeout=3.0)
    assert calls == ["other"]

    aggregate.clear_failure_cache()

    calls.clear()
    aggregate.search_all("晴天", timeout=3.0)
    assert sorted(calls) == ["other", "p"]
    assert aggregate.failed_platforms() == []


def test_when_every_platform_is_banned_one_full_retry_is_allowed(
    stats_home: Path, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale stats file must never leave the user with a guaranteed empty page."""
    calls = _providers(monkeypatch, ["a", "b"])

    class Provider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            return [_track(keyword, "x")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return Provider()

    monkeypatch.setattr(aggregate, "create", create)
    aggregate._record_failure("a", 10)
    aggregate._record_failure("b", 10)
    assert aggregate.failed_platforms() == ["a", "b"]

    result = aggregate.search_all("晴天", timeout=3.0)

    assert calls == ["a", "b"]
    assert {item.platform for item in result.platforms} == {"a", "b"}
    assert all(item.ok for item in result.platforms)
    # The cache was wiped: nothing is banned afterwards.
    assert aggregate.failed_platforms() == []
