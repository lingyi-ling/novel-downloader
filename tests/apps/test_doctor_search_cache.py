#!/usr/bin/env python3
"""
Doctor search-cache diagnostics contract.

``novel-cli doctor`` reports the search cache through
:meth:`novel_downloader.plugins.search_cache.SearchCache.diagnostics`.  The
row is advisory, read-only, and deliberately key-free: it exposes counters and
an opaque configuration fingerprint, never a keyword, site selection or
filter.

These tests pin that contract end to end:

* an empty cache reports the initial values with the documented types;
* after real cache operations (hit, miss, expiry, clear) every reported value
  equals a fresh ``diagnostics()`` snapshot;
* an unreadable directory, a corrupt payload and a failing diagnostics call
  are reported as explainable states instead of crashing the command;
* ``--json`` and the human row agree on the fields the human row shows, and
  neither leaks the cached keyword;
* ``NOVELDL_SEARCH_CACHE_DIR`` is honoured by a fresh doctor process, which
  counts payloads in exactly that directory;
* doctor only reads: it changes neither payload bytes nor counters, including
  while other threads are writing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_downloader.apps.cli.commands import doctor
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd
from novel_downloader.plugins.search_cache import (
    DISK_DIR_ENV,
    CacheDiagnostics,
    SearchCache,
    build_key,
)
from novel_downloader.schemas import SearchResult

#: Module invoked by the subprocess harness (same entry point as ``novel-cli``).
CLI_MODULE = "novel_downloader.apps.cli.main"

#: Fingerprint used when a test replaces the registry-derived value.
FINGERPRINT = "0123456789abcdef"

#: The fields the human-readable row and ``--json`` both expose.
SHARED_FIELDS = ("memory_entries", "disk_entries", "hits", "expired_entries")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """A manually advanced clock used for TTL and persisted timestamps."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _row(book_id: str) -> SearchResult:
    row: dict[str, object] = {
        "site": "alpha",
        "book_id": book_id,
        "book_url": f"https://example.com/alpha/{book_id}",
        "cover_url": "",
        "title": f"title-{book_id}",
        "author": "author",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }
    return cast("SearchResult", row)


def _disk_cache(
    tmp_path: Path,
    clock: _Clock,
    *,
    ttl: float = 60.0,
    max_entries: int = 64,
) -> SearchCache:
    """Return a disk-backed cache rooted inside ``tmp_path``."""
    return SearchCache(
        ttl=ttl,
        max_entries=max_entries,
        now=clock,
        wall_clock=clock,
        disk_dir=tmp_path / "search-cache",
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    cache: SearchCache,
    *,
    fingerprint: str = FINGERPRINT,
) -> None:
    """Point doctor at ``cache`` and pin the registry-derived fingerprint."""
    monkeypatch.setattr(doctor, "_search_cache", lambda: cache)
    monkeypatch.setattr(doctor, "_search_cache_fingerprint", lambda: fingerprint)


def _expected(
    snapshot: CacheDiagnostics,
    *,
    fingerprint: str = FINGERPRINT,
) -> dict[str, Any]:
    """Return the exact payload ``_check_search_cache`` must attach."""
    return {**asdict(snapshot), "fingerprint": fingerprint}


def _doctor_data() -> dict[str, Any]:
    """Return the current ``search_cache`` row's structured payload."""
    check = doctor._check_search_cache()
    assert check.ok is True
    assert check.fatal is False
    assert check.data is not None
    return dict(check.data)


def _assert_int(value: object, field: str) -> None:
    assert isinstance(value, int) and not isinstance(value, bool), field


def _assert_types(data: dict[str, Any]) -> None:
    for field in (
        "memory_entries",
        "expired_entries",
        "disk_entries",
        "hits",
        "misses",
        "evictions",
    ):
        _assert_int(data[field], field)
    assert isinstance(data["disk_enabled"], bool)
    assert isinstance(data["fingerprint"], str)


class _Messages:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []


def _capture_ui(monkeypatch: pytest.MonkeyPatch, module: Any) -> _Messages:
    messages = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(module.ui, name, getattr(messages, name).append)
    return messages


def _doctor_args(*, as_json: bool = False) -> SimpleNamespace:
    # ``collect_checks`` is replaced in these tests, so the port is never bound.
    return SimpleNamespace(port=1, as_json=as_json, online=False)


# ---------------------------------------------------------------------------
# Empty cache: documented initial values and types
# ---------------------------------------------------------------------------


def test_empty_cache_reports_initial_values_with_documented_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock)
    _install(monkeypatch, cache)

    data = _doctor_data()

    assert data == _expected(cache.diagnostics())
    assert data["memory_entries"] == 0
    assert data["expired_entries"] == 0
    assert data["disk_enabled"] is True
    assert data["disk_entries"] == 0
    assert data["hits"] == 0
    assert data["misses"] == 0
    assert data["evictions"] == 0
    _assert_types(data)

    check = doctor._check_search_cache()
    assert check.detail.startswith("0 memory / 0 disk entry(s), 0 hit(s), 0 expired")
    assert f"(fingerprint {FINGERPRINT})" in check.detail
    assert not (tmp_path / "search-cache").exists()  # diagnostics never creates it


def test_empty_memory_only_cache_reports_disk_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SearchCache(disk_dir="", now=_Clock(), wall_clock=_Clock())
    _install(monkeypatch, cache)

    data = _doctor_data()

    assert data == _expected(cache.diagnostics())
    assert data["disk_enabled"] is False
    assert data["disk_entries"] == 0


def test_empty_cache_fingerprint_matches_the_registry_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a patch, the row reports whatever the real source returns.

    A degraded registry makes the source raise; doctor then leaves the field
    empty and stays advisory rather than failing the check.
    """
    cache = _disk_cache(tmp_path, _Clock())
    monkeypatch.setattr(doctor, "_search_cache", lambda: cache)

    try:
        expected = doctor._search_cache_fingerprint()
    except Exception:
        expected = ""

    data = doctor._check_search_cache().data or {}

    assert data["fingerprint"] == expected
    assert isinstance(data["fingerprint"], str)
    if expected:
        assert re.fullmatch(r"[0-9a-f]{16}", expected)


# ---------------------------------------------------------------------------
# Real operations: the row equals a fresh diagnostics() snapshot
# ---------------------------------------------------------------------------


def test_doctor_values_track_hits_misses_expiry_and_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock, ttl=10.0, max_entries=2)
    _install(monkeypatch, cache)

    first = build_key("alpha one", ["alpha"])
    second = build_key("alpha two", ["alpha"])
    third = build_key("alpha three", ["alpha"])
    absent = build_key("never cached", ["alpha"])

    cache.put(first, [_row("1")])
    cache.put(second, [_row("2")])
    assert cache.get(first) is not None  # hit
    assert cache.get(absent) is None  # miss
    cache.put(third, [_row("3")])  # ``second`` is least recently used -> eviction

    live = cache.diagnostics()
    assert (live.memory_entries, live.expired_entries) == (2, 0)
    assert (live.hits, live.misses, live.evictions) == (1, 1, 1)
    assert live.disk_entries == 3
    assert _doctor_data() == _expected(live)

    # Reach the TTL: diagnostics counts expired entries without purging them.
    clock.advance(10.0)
    expired = cache.diagnostics()
    assert (expired.memory_entries, expired.expired_entries) == (2, 2)
    assert expired.disk_entries == 3
    data = _doctor_data()
    assert data == _expected(expired)
    assert data["expired_entries"] == 2
    assert "2 memory / 3 disk entry(s), 1 hit(s), 2 expired" in (
        doctor._check_search_cache().detail
    )

    # Clearing drops entries and disk payloads but keeps cumulative counters.
    cache.clear()
    cleared = cache.diagnostics()
    assert (cleared.memory_entries, cleared.expired_entries) == (0, 0)
    assert cleared.disk_entries == 0
    assert (cleared.hits, cleared.misses, cleared.evictions) == (1, 1, 1)
    assert _doctor_data() == _expected(cleared)


# ---------------------------------------------------------------------------
# Broken disk state: explainable, never a crash
# ---------------------------------------------------------------------------


def test_unreadable_disk_directory_reports_zero_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _disk_cache(tmp_path, _Clock())
    target = cache.disk_dir
    assert target is not None
    target.mkdir()

    original_glob = Path.glob

    def _deny(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        if os.path.normcase(str(self)) == os.path.normcase(str(target)):
            raise PermissionError("permission denied by test")
        return original_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", _deny)
    _install(monkeypatch, cache)

    check = doctor._check_search_cache()

    assert check.ok is True
    assert check.fatal is False
    data = dict(check.data or {})
    assert data["disk_enabled"] is True
    assert data["disk_entries"] == 0  # the documented fallback for an unreadable dir
    assert "0 disk entry(s)" in check.detail
    assert cache.diagnostics().disk_entries == 0


def test_corrupt_payload_is_counted_but_never_opened_by_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock)
    key = build_key("corrupt payload query", ["alpha"])
    cache.put(key, [_row("1")])

    payload = next((tmp_path / "search-cache").glob("v1-*.json"))
    payload.write_text('{"version": 1, "key": ', encoding="utf-8")  # truncated

    reader = _disk_cache(tmp_path, clock)
    assert reader.get(key) is None  # a corrupt file is a miss, not an error
    assert reader.diagnostics().misses == 1
    assert reader.diagnostics().disk_entries == 1

    _install(monkeypatch, reader)
    check = doctor._check_search_cache()

    assert check.ok is True
    data = dict(check.data or {})
    assert data == _expected(reader.diagnostics())
    assert data["disk_entries"] == 1
    assert data["misses"] == 1
    # The truncated file is still on disk: doctor neither repairs nor deletes.
    assert payload.exists()


def test_diagnostics_failure_becomes_an_advisory_unavailable_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnreadableCache:
        def diagnostics(self) -> CacheDiagnostics:
            raise PermissionError("permission denied by test")

    monkeypatch.setattr(doctor, "_search_cache", lambda: _UnreadableCache())
    monkeypatch.setattr(doctor, "_search_cache_fingerprint", lambda: FINGERPRINT)

    check = doctor._check_search_cache()

    assert check.ok is False
    assert check.fatal is False  # an optional row never fails the command
    assert "unavailable" in check.detail
    assert "permission denied by test" in check.detail
    assert check.data is None


# ---------------------------------------------------------------------------
# --json and the human row share the same numbers, and leak no keyword
# ---------------------------------------------------------------------------


def test_json_and_human_rows_agree_and_never_leak_the_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyword = "private-vault-query-9c1f"
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock, ttl=10.0)
    _install(monkeypatch, cache)

    key = build_key(keyword, ["alpha"], min_words=1000)
    cache.put(key, [_row("1")])
    assert cache.get(key) is not None
    assert cache.get(build_key("other private query", ["alpha"])) is None
    clock.advance(10.0)

    expected = _expected(cache.diagnostics())
    check = doctor._check_search_cache()
    assert check.data == expected

    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: [check])

    messages = _capture_ui(monkeypatch, doctor)
    DoctorCmd.run(_doctor_args())

    human = "\n".join(
        [*messages.success, *messages.info, *messages.warn, *messages.error]
    )
    for phrase in (keyword, "other private query"):
        assert phrase not in human
        assert phrase not in json.dumps(expected, ensure_ascii=False)

    # The human row shows memory/disk/hits/expired plus the fingerprint.
    for field in SHARED_FIELDS:
        assert str(expected[field]) in human
    assert f"(fingerprint {FINGERPRINT})" in human
    assert (
        f"{expected['memory_entries']} memory / {expected['disk_entries']} "
        f"disk entry(s), {expected['hits']} hit(s), "
        f"{expected['expired_entries']} expired"
    ) in human

    DoctorCmd.run(_doctor_args(as_json=True))
    output = capsys.readouterr().out
    payload = json.loads(output)
    row = next(item for item in payload["checks"] if item["key"] == "search_cache")

    assert row["ok"] is True
    assert row["fatal"] is False
    assert row["data"] == expected
    _assert_types(row["data"])
    # ``--json`` carries the complete documented field set ...
    assert set(row["data"]) == {
        "memory_entries",
        "expired_entries",
        "disk_enabled",
        "disk_entries",
        "hits",
        "misses",
        "evictions",
        "fingerprint",
    }
    # ... and every field the human row shows has the same value there.
    for field in SHARED_FIELDS:
        assert row["data"][field] == expected[field]
    assert row["data"]["fingerprint"] == expected["fingerprint"]
    for phrase in (keyword, "other private query"):
        assert phrase not in output


# ---------------------------------------------------------------------------
# NOVELDL_SEARCH_CACHE_DIR: a fresh doctor process reads that directory
# ---------------------------------------------------------------------------


def _parse_doctor_json(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "doctor --json printed no document"
    return cast("dict[str, Any]", json.loads(lines[-1]))


def _search_cache_row(payload: dict[str, Any]) -> dict[str, Any]:
    row = next(
        (item for item in payload["checks"] if item["key"] == "search_cache"), None
    )
    assert row is not None
    return cast("dict[str, Any]", row)


def _run_doctor_json(
    tmp_path: Path,
    *,
    cache_dir: Path | None,
) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "NOVEL_HOME": str(home),
            "NOVEL_LANG": "en_US",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "COLUMNS": "200",
        }
    )
    if cache_dir is None:
        env.pop(DISK_DIR_ENV, None)
    else:
        env[DISK_DIR_ENV] = str(cache_dir)
    result = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "doctor", "--json"],
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240.0,
        check=False,
    )
    return _parse_doctor_json(result.stdout)


def test_custom_env_dir_is_read_by_a_fresh_doctor_process(tmp_path: Path) -> None:
    cache_dir = tmp_path / "custom-search-cache"
    cache_dir.mkdir()
    (cache_dir / f"v1-{'a' * 64}.json").write_text("{}", encoding="utf-8")
    (cache_dir / f"v1-{'b' * 64}.json").write_text("{}", encoding="utf-8")
    # Files outside this cache's format are never counted.
    (cache_dir / f"v2-{'c' * 64}.json").write_text("{}", encoding="utf-8")
    (cache_dir / "notes.json").write_text("{}", encoding="utf-8")

    configured = _search_cache_row(_run_doctor_json(tmp_path, cache_dir=cache_dir))
    data = dict(configured["data"])

    assert data["disk_enabled"] is True
    assert data["disk_entries"] == 2
    assert (data["memory_entries"], data["hits"], data["misses"]) == (0, 0, 0)
    assert configured["fatal"] is False

    # Without the variable the same command is the default memory-only mode.
    default = _search_cache_row(_run_doctor_json(tmp_path, cache_dir=None))
    default_data = dict(default["data"])
    assert default_data["disk_enabled"] is False
    assert default_data["disk_entries"] == 0


# ---------------------------------------------------------------------------
# Read-only guarantee, including concurrent writers
# ---------------------------------------------------------------------------


def test_doctor_does_not_change_payloads_or_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock)
    key = build_key("read only query", ["alpha"])
    cache.put(key, [_row("1")])
    assert cache.get(key) is not None

    directory = cache.disk_dir
    assert directory is not None
    before = cache.diagnostics()
    before_files = {
        path.name: path.read_bytes() for path in directory.glob("v1-*.json")
    }
    assert before_files

    _install(monkeypatch, cache)
    for _ in range(5):
        data = _doctor_data()
        assert data == _expected(before)

    assert cache.diagnostics() == before
    after_files = {path.name: path.read_bytes() for path in directory.glob("v1-*.json")}
    assert after_files == before_files
    assert not list(directory.glob("*.tmp"))
    assert not list(directory.glob(".*.tmp"))


def test_doctor_snapshot_stays_read_only_while_writers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock)
    keys = [build_key(f"concurrent-{index}", ["alpha"]) for index in range(6)]
    directory = cache.disk_dir
    assert directory is not None

    failures: list[Exception] = []

    def writer(index: int) -> None:
        try:
            for step in range(60):
                cache.put(keys[index], [_row(f"{index}-{step}")])
        except Exception as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()

    _install(monkeypatch, cache)
    snapshots: list[dict[str, Any]] = []
    for _ in range(30):
        check = doctor._check_search_cache()
        assert check.ok is True
        assert check.fatal is False
        data = dict(check.data or {})
        _assert_types(data)
        # Writers only put; they never read, and six keys stay under capacity.
        assert (data["hits"], data["misses"], data["evictions"]) == (0, 0, 0)
        snapshots.append(data)

    for thread in threads:
        thread.join(timeout=30.0)
        assert not thread.is_alive()
    assert failures == []

    final = cache.diagnostics()
    assert (final.hits, final.misses, final.evictions) == (0, 0, 0)
    assert final.memory_entries == len(keys)
    # Doctor never opened or rewrote a payload: every file is still complete.
    payloads = sorted(directory.glob("v1-*.json"))
    assert len(payloads) == len(keys)
    for path in payloads:
        json.loads(path.read_bytes())
    assert not list(directory.glob("*.tmp"))
    assert not list(directory.glob(".*.tmp"))
    assert snapshots  # the snapshot loop actually ran


# ---------------------------------------------------------------------------
# The row is wired into collect_checks as an optional check
# ---------------------------------------------------------------------------


def test_collect_checks_contains_the_optional_search_cache_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = _disk_cache(tmp_path, _Clock())
    _install(monkeypatch, cache)
    monkeypatch.setattr(
        doctor,
        "_check_site_plugins",
        lambda: Check("Site plugins", True, key="site_plugins"),
    )
    monkeypatch.setattr(
        doctor, "_check_searchers", lambda: Check("Searchers", True, key="searchers")
    )
    # Keep the directory probes (which create directories) inside tmp_path.
    monkeypatch.setattr(doctor, "_output_dir", lambda: tmp_path / "downloads")
    monkeypatch.setattr(doctor, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(doctor, "_cache_dir", lambda: tmp_path / "cache")

    row = next(
        check for check in doctor.collect_checks(port=1) if check.key == "search_cache"
    )

    assert isinstance(row, Check)
    assert row.ok is True
    assert row.fatal is False
    assert row.elapsed is not None
    assert row.data == _expected(cache.diagnostics())
