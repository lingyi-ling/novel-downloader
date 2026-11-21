#!/usr/bin/env python3
"""
Desktop search-cache panel: clear semantics, failure handling and contracts.

The settings page exposes two bridge calls:

* ``get_search_cache_stats`` -- a read-only :meth:`SearchCache.diagnostics`
  snapshot rendered as four counters plus a status note;
* ``clear_search_cache`` -- the explicit, two-click destructive action.

This module pins the behaviour behind those calls against the real
:class:`SearchCache` (not a stub), because the panel's value is exactly that
it reports what the cache actually did:

* a confirmed clear removes only this cache format's payloads and keeps the
  cumulative hit/miss/eviction counters and every unrelated file;
* a search that writes while a clear runs never leaves a half-written file,
  and a payload replaced after the clear enumerated it is left alone;
* an unreadable, unwritable or locked directory turns into the documented
  ``{"ok": False, "error": ...}`` answer instead of an exception escaping a
  pywebview worker thread, and the page still renders a coherent state;
* diagnostics reflect the clear, repeated/concurrent clears are idempotent,
  ``NOVELDL_SEARCH_CACHE_DIR`` is honoured, and both the bridge payload and
  the ``doctor --json`` row keep a stable field set.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import novel_downloader.apps.desktop.webview_app as wa
from novel_downloader.apps.cli.commands import doctor
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.apps.desktop.webview_app import ApiBridge
from novel_downloader.plugins.search_cache import (
    DISK_DIR_ENV,
    SearchCache,
    build_key,
)
from novel_downloader.schemas import SearchResult

#: Fields the diagnostics bridge always answers, in addition to ``ok``/``error``.
STATS_FIELDS = {
    "memory_entries",
    "expired_entries",
    "disk_entries",
    "disk_enabled",
    "hits",
    "misses",
    "evictions",
}

#: Fields the ``doctor --json`` row adds to the shared counters.
DOCTOR_EXTRA_FIELDS = {"fingerprint"}


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


def _row(book_id: str, site: str = "alpha") -> SearchResult:
    row: dict[str, object] = {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.com/{site}/{book_id}",
        "cover_url": "",
        "title": f"title-{book_id}",
        "author": "author",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }
    return cast("SearchResult", row)


def _disk_cache(directory: Path, clock: _Clock, *, ttl: float = 60.0) -> SearchCache:
    """Return a disk-backed cache rooted in ``directory``."""
    return SearchCache(ttl=ttl, now=clock, wall_clock=clock, disk_dir=directory)


def _install(monkeypatch: pytest.MonkeyPatch, cache: SearchCache) -> ApiBridge:
    """Point the desktop bridge at ``cache`` and return a fresh bridge."""
    monkeypatch.setattr(wa, "_shared_search_cache", lambda: cache)
    return ApiBridge()


def _payloads(directory: Path) -> list[Path]:
    """Return the regular payload files (a matching *directory* is not one)."""
    return sorted(path for path in directory.glob("v1-*.json") if path.is_file())


def _assert_no_temp_files(directory: Path) -> None:
    assert not list(directory.glob("*.tmp"))
    assert not list(directory.glob(".*.tmp"))


def _js_handler(needle: str) -> str:
    """Return the JS statement block starting at ``needle``."""
    start = APP_HTML.index(needle)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


class _ScandirHook:
    """
    Run ``action`` once the hooked directory has been fully enumerated.

    ``SearchCache._disk_clear`` lists the directory, then re-checks every
    candidate's identity before unlinking it.  Firing between those two steps
    models a concurrent ``put`` landing in exactly the window the identity
    check exists for.
    """

    def __init__(self, target: Path, action: Callable[[], None]) -> None:
        self._target = os.path.normcase(os.path.abspath(str(target)))
        self._action = action
        self._real = os.scandir
        self._fired = False

    def __call__(self, path: Any = ".") -> Any:
        context = self._real(path)
        if os.path.normcase(os.path.abspath(str(path))) != self._target:
            return context
        return _ScandirContext(context, self)

    def fire(self) -> None:
        if not self._fired:
            self._fired = True
            self._action()


class _ScandirContext:
    def __init__(self, context: Any, hook: _ScandirHook) -> None:
        self._context = context
        self._hook = hook

    def __enter__(self) -> Any:
        return self._context.__enter__()

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return self._context.__exit__(*exc_info)
        finally:
            self._hook.fire()


def _write_payload_atomically(path: Path, payload: dict[str, object]) -> None:
    """Replace ``path`` with a complete payload (a concurrent ``put``)."""
    tmp = path.with_name(f".{path.name}.concurrent.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Two-step confirmation and clear semantics
# ---------------------------------------------------------------------------


def test_two_step_confirmation_arms_without_calling_the_bridge() -> None:
    """The first click only arms; the bridge is reached by the second one."""
    handler = _js_handler('$("#btn-set-clear-search-cache").onclick')

    arm = handler.index("if (!clearCacheArmed)")
    early_return = handler.index("return;", arm)
    bridge_call = handler.index('call("clear_search_cache")')

    assert arm < early_return < bridge_call
    assert "clearCacheTimer = setTimeout(disarmClearSearchCache, 4000);" in handler
    assert "setClearSearchCacheLabel(true);" in handler


def test_failed_clear_re_enables_the_button_and_shows_the_error() -> None:
    """A structured failure is toasted; the button never stays disabled."""
    handler = _js_handler('$("#btn-set-clear-search-cache").onclick')

    assert "button.disabled = true;" in handler
    disable = handler.index("button.disabled = true;")
    enable = handler.index("button.disabled = false;", disable)
    failure = handler.index("if (!r || r.ok !== true)")
    success_toast = handler.index('toast(t("Search cache cleared"), "ok")')
    refresh = handler.index("await refreshSearchCacheStats();")

    assert disable < enable < failure < success_toast < refresh
    assert 't("Could not clear the search cache")' in handler
    assert "return;" in handler[failure:success_toast]


def test_confirmed_clear_removes_only_payloads_and_keeps_everything_else(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    directory = tmp_path / "search-cache"
    cache = _disk_cache(directory, clock)
    first = build_key("清空 一", ["alpha"])
    second = build_key("清空 二", ["beta"])
    cache.put(first, [_row("1")])
    cache.put(second, [_row("2")])
    assert cache.get(first) == [_row("1")]  # one recorded hit
    assert len(_payloads(directory)) == 2

    unrelated = {
        "v1-notes.txt": "keep-v1-text",
        "v2-old.json": "keep-v2",
        "notes.json": "keep-notes",
        ".v1-tmp.json.tmp": "keep-tmp",
    }
    for name, content in unrelated.items():
        (directory / name).write_text(content, encoding="utf-8")
    # A directory whose name matches the payload pattern must never be
    # unlinked (on Windows ``unlink`` on a directory raises).
    (directory / "v1-fake.json").mkdir()

    before = cache.stats()
    bridge = _install(monkeypatch, cache)

    result = bridge.clear_search_cache()

    assert result == {"ok": True, "error": None}
    assert _payloads(directory) == []
    for name, content in unrelated.items():
        assert (directory / name).read_text(encoding="utf-8") == content
    assert (directory / "v1-fake.json").is_dir()
    assert cache.stats() == before
    assert cache.diagnostics().memory_entries == 0
    assert cache.diagnostics().disk_entries == 0


def test_clear_of_a_missing_directory_is_a_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _disk_cache(tmp_path / "never-created", _Clock())
    bridge = _install(monkeypatch, cache)

    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert cache.diagnostics().disk_enabled is True
    assert cache.diagnostics().disk_entries == 0


# ---------------------------------------------------------------------------
# Diagnostics after a clear
# ---------------------------------------------------------------------------


def test_diagnostics_reflect_the_clear_and_keep_cumulative_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path / "cache", clock, ttl=10.0)
    live = build_key("live", ["alpha"])
    expired = build_key("expired", ["alpha"])
    cache.put(live, [_row("1")])
    cache.put(expired, [_row("2")])
    assert cache.get(live) is not None
    assert cache.get(build_key("absent", ["alpha"])) is None
    clock.advance(10.0)  # both entries reach the TTL

    bridge = _install(monkeypatch, cache)
    before = bridge.get_search_cache_stats()
    assert before["ok"] is True
    assert before["memory_entries"] == 2
    assert before["expired_entries"] == 2
    assert before["disk_entries"] == 2
    assert (before["hits"], before["misses"]) == (1, 1)

    assert bridge.clear_search_cache()["ok"] is True

    after = bridge.get_search_cache_stats()
    assert after["ok"] is True
    assert after["error"] is None
    assert after["memory_entries"] == 0
    assert after["expired_entries"] == 0
    assert after["disk_entries"] == 0
    # Clearing entries is not a usage event: the counters survive.
    assert (after["hits"], after["misses"], after["evictions"]) == (1, 1, 0)


# ---------------------------------------------------------------------------
# Failure modes: unreadable, unwritable, locked
# ---------------------------------------------------------------------------


def test_unreadable_directory_reports_a_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    key = build_key("unreadable", ["alpha"])
    cache.put(key, [_row("1")])
    payload = _payloads(directory)[0]

    original_scandir = os.scandir

    def _deny(path: Any = ".") -> Any:
        if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(
            os.path.abspath(str(directory))
        ):
            raise PermissionError(13, "permission denied by test")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", _deny)
    bridge = _install(monkeypatch, cache)

    result = bridge.clear_search_cache()

    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert "PermissionError" in result["error"]
    # Memory is always dropped, even when the disk half fails.
    assert cache.diagnostics().memory_entries == 0
    # The payload could not be removed, and the failure said so.  (``glob``
    # swallows the simulated error, so the check goes through the real path.)
    assert payload.exists()

    # The diagnostics read still answers a coherent payload.
    stats = bridge.get_search_cache_stats()
    assert stats["ok"] is True
    assert stats["memory_entries"] == 0
    assert set(stats) == STATS_FIELDS | {"ok", "error"}


def test_unwritable_payload_reports_a_structured_error_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    cache.put(build_key("locked", ["alpha"]), [_row("1")])
    payload = _payloads(directory)[0]
    bridge = _install(monkeypatch, cache)

    original_unlink = Path.unlink

    def _deny(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == payload:
            raise PermissionError(13, "access denied by test")
        original_unlink(self, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", _deny)
        result = bridge.clear_search_cache()

    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert "PermissionError" in result["error"]
    assert payload.exists()
    assert cache.diagnostics().memory_entries == 0

    # Once the obstruction is gone the same click succeeds and is idempotent.
    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert _payloads(directory) == []
    assert bridge.clear_search_cache() == {"ok": True, "error": None}


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation")
def test_payload_held_open_on_windows_is_reported_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file another process holds open cannot be unlinked; say so."""
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    cache.put(build_key("held", ["alpha"]), [_row("1")])
    payload = _payloads(directory)[0]
    bridge = _install(monkeypatch, cache)

    handle = payload.open("rb")
    try:
        result = bridge.clear_search_cache()
    finally:
        handle.close()

    assert result["ok"] is False
    assert "PermissionError" in result["error"]
    assert payload.exists()
    # The page can still render the (now memory-empty) state.
    assert bridge.get_search_cache_stats()["ok"] is True

    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert _payloads(directory) == []


# ---------------------------------------------------------------------------
# Concurrency: writers during a clear, repeated clears
# ---------------------------------------------------------------------------


def test_concurrent_writes_during_clear_leave_only_complete_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Searches keep writing while the user clears; nothing is corrupted."""
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    keys = [build_key(f"concurrent-{index}", ["alpha"]) for index in range(4)]
    bridge = _install(monkeypatch, cache)
    failures: list[BaseException] = []
    barrier = threading.Barrier(5)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            for step in range(40):
                cache.put(keys[index], [_row(f"{index}-{step}")])
        except BaseException as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    def clearer() -> None:
        try:
            barrier.wait(timeout=10.0)
            for _ in range(15):
                result = bridge.clear_search_cache()
                # A writer replacing a payload at the same instant can make
                # ``unlink`` fail with a Windows sharing violation.  That is
                # reported, not raised, and the next click converges; what
                # must never happen is a crash or a corrupted file.
                assert set(result) == {"ok", "error"}
                assert result["ok"] is True or "PermissionError" in result["error"]
        except BaseException as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)] + [
        threading.Thread(target=clearer)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)

    # Whatever survived is a complete, valid payload of this format.
    for path in _payloads(directory):
        payload = json.loads(path.read_bytes())
        assert payload["version"] == 1
    _assert_no_temp_files(directory)

    # One last clear converges to the documented empty state.
    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert _payloads(directory) == []
    stats = bridge.get_search_cache_stats()
    assert (stats["memory_entries"], stats["disk_entries"]) == (0, 0)
    assert stats["hits"] == 0 and stats["misses"] == 0


def test_payload_replaced_after_enumeration_is_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot written after the clear listed the directory survives."""
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    key = build_key("replaced", ["alpha"])
    cache.put(key, [_row("1")])
    payload = _payloads(directory)[0]
    replacement = {
        "version": 1,
        "key": "concurrent-writer",
        "stored_at": clock(),
        "results": [_row("new-1"), _row("new-2")],
    }

    hook = _ScandirHook(
        directory, lambda: _write_payload_atomically(payload, replacement)
    )
    with monkeypatch.context() as context:
        context.setattr(os, "scandir", hook)
        cache.clear()

    assert payload.exists(), "the newer snapshot must not be deleted"
    assert json.loads(payload.read_bytes()) == replacement
    assert cache.diagnostics().memory_entries == 0
    _assert_no_temp_files(directory)


def test_payload_created_after_enumeration_is_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files the clear never listed are outside its remit."""
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    cache.put(build_key("old", ["alpha"]), [_row("1")])
    late = directory / f"v1-{'f' * 64}.json"
    payload = {"version": 1, "key": "late", "stored_at": clock(), "results": []}

    hook = _ScandirHook(directory, lambda: late.write_text(json.dumps(payload)))
    with monkeypatch.context() as context:
        context.setattr(os, "scandir", hook)
        cache.clear()

    assert late.exists()
    assert _payloads(directory) == [late]


def test_repeated_and_concurrent_clears_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    directory = tmp_path / "cache"
    cache = _disk_cache(directory, clock)
    for index in range(3):
        cache.put(build_key(f"idempotent-{index}", ["alpha"]), [_row(str(index))])
    assert cache.get(build_key("idempotent-0", ["alpha"])) is not None
    bridge = _install(monkeypatch, cache)

    for _ in range(3):
        assert bridge.clear_search_cache() == {"ok": True, "error": None}

    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def click() -> None:
        try:
            barrier.wait(timeout=10.0)
            results.append(bridge.clear_search_cache())
        except BaseException as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert results == [{"ok": True, "error": None}] * 8
    assert _payloads(directory) == []
    assert cache.diagnostics().disk_entries == 0
    # One recorded hit before the clears; clearing never invents usage.
    assert cache.stats().hits == 1


# ---------------------------------------------------------------------------
# NOVELDL_SEARCH_CACHE_DIR
# ---------------------------------------------------------------------------


def test_custom_env_dir_is_the_only_directory_cleared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "custom-search-cache"
    other = tmp_path / "unrelated-cache"
    other.mkdir()
    (other / f"v1-{'a' * 64}.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv(DISK_DIR_ENV, str(target))
    clock = _Clock()
    # No ``disk_dir``: the environment variable decides.
    cache = SearchCache(ttl=60.0, now=clock, wall_clock=clock)
    assert cache.disk_dir == target

    cache.put(build_key("env", ["alpha"]), [_row("1")])
    assert len(_payloads(target)) == 1
    bridge = _install(monkeypatch, cache)

    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert _payloads(target) == []
    assert (other / f"v1-{'a' * 64}.json").exists()
    assert bridge.get_search_cache_stats()["disk_enabled"] is True


def test_unset_env_keeps_the_panel_memory_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DISK_DIR_ENV, raising=False)
    cache = SearchCache(ttl=60.0, now=_Clock(), wall_clock=_Clock())
    assert cache.disk_dir is None
    cache.put(build_key("memory", ["alpha"]), [_row("1")])

    bridge = _install(monkeypatch, cache)
    stats = bridge.get_search_cache_stats()
    assert stats["disk_enabled"] is False
    assert stats["disk_entries"] == 0
    assert stats["memory_entries"] == 1

    assert bridge.clear_search_cache() == {"ok": True, "error": None}
    assert bridge.get_search_cache_stats()["memory_entries"] == 0
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Stable field sets: bridge payload and ``doctor --json``
# ---------------------------------------------------------------------------


def test_bridge_payload_fields_are_stable_and_json_serialisable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _disk_cache(tmp_path / "cache", _Clock())
    bridge = _install(monkeypatch, cache)

    stats = bridge.get_search_cache_stats()
    assert set(stats) == STATS_FIELDS | {"ok", "error"}
    assert stats["ok"] is True
    assert stats["error"] is None
    for field in STATS_FIELDS - {"disk_enabled"}:
        assert type(stats[field]) is int, field
    assert type(stats["disk_enabled"]) is bool

    cleared = bridge.clear_search_cache()
    assert set(cleared) == {"ok", "error"}
    assert cleared == {"ok": True, "error": None}

    # Both answers cross the pywebview boundary as JSON.
    assert json.loads(json.dumps(stats)) == stats
    assert json.loads(json.dumps(cleared)) == cleared


def test_doctor_json_row_shares_the_panel_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI ``--json`` row and the panel describe the same cache."""
    clock = _Clock()
    cache = _disk_cache(tmp_path / "cache", clock)
    cache.put(build_key("json", ["alpha"]), [_row("1")])
    monkeypatch.setattr(doctor, "_search_cache", lambda: cache)
    monkeypatch.setattr(doctor, "_search_cache_fingerprint", lambda: "0123456789abcdef")

    check = doctor._check_search_cache()
    assert check.ok is True
    assert check.fatal is False
    row = check.to_dict()
    data = cast("dict[str, Any]", row["data"])

    assert set(row) == {"key", "name", "ok", "fatal", "detail", "data"}
    assert set(data) == STATS_FIELDS | DOCTOR_EXTRA_FIELDS
    assert json.loads(json.dumps(row)) == row

    bridge = _install(monkeypatch, cache)
    stats = bridge.get_search_cache_stats()
    for field in STATS_FIELDS:
        assert stats[field] == data[field], field
