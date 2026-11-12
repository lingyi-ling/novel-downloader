#!/usr/bin/env python3
"""Tests for the browser-assisted search helper and its orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import types
from typing import Any

import pytest

import novel_downloader.plugins.search as search_module
from novel_downloader.apps.browser import (
    DEFAULT_BROWSER_ASSISTED_SITES,
    MAX_BROWSER_SEARCH_TIMEOUT,
    BrowserSearchResult,
    BrowserSearchSpec,
    browser_search_main,
    capture_search_json,
    parse_browser_assisted_sites,
    resolve_spec,
    run_browser_search,
    run_browser_search_detailed,
    search_via_browser,
    search_via_browser_result,
)
from novel_downloader.apps.browser import main as browser_main
from novel_downloader.apps.browser import settings as browser_settings
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

# --------------------------------------------------------------------------- #
# Configuration parsing
# --------------------------------------------------------------------------- #


def test_default_browser_assisted_sites() -> None:
    assert DEFAULT_BROWSER_ASSISTED_SITES == ("fanqienovel",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["FanqieNovel", " fanqienovel ", "other"], ("fanqienovel", "other")),
        ("fanqienovel, Other ,,", ("fanqienovel", "other")),
        ((), ()),
        ([], ()),
        ("", ()),
        (["fanqienovel", 3, None], ("fanqienovel",)),
    ],
)
def test_parse_browser_assisted_sites(value: Any, expected: tuple[str, ...]) -> None:
    assert parse_browser_assisted_sites(value) == expected


@pytest.mark.parametrize("value", [None, 42, {"site": "fanqienovel"}])
def test_parse_browser_assisted_sites_rejects_unusable_values(value: Any) -> None:
    assert parse_browser_assisted_sites(value) is None


def test_browser_assisted_sites_defaults_when_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_settings, "load_config", lambda *a, **k: {"general": {}}
    )
    assert browser_settings.browser_assisted_sites() == DEFAULT_BROWSER_ASSISTED_SITES


def test_browser_assisted_sites_reads_configured_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_settings,
        "load_config",
        lambda *a, **k: {
            "general": {"browser_assisted_sites": ["fanqienovel", "example"]}
        },
    )
    assert browser_settings.browser_assisted_sites() == ("fanqienovel", "example")


def test_browser_assisted_sites_empty_list_disables_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_settings,
        "load_config",
        lambda *a, **k: {"general": {"browser_assisted_sites": []}},
    )
    assert browser_settings.browser_assisted_sites() == ()


def test_browser_assisted_sites_falls_back_on_config_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*a: Any, **k: Any) -> dict[str, Any]:
        raise FileNotFoundError("no config")

    monkeypatch.setattr(browser_settings, "load_config", _missing)
    assert browser_settings.browser_assisted_sites() == DEFAULT_BROWSER_ASSISTED_SITES


def test_browser_assisted_sites_falls_back_on_invalid_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_settings,
        "load_config",
        lambda *a, **k: {"general": {"browser_assisted_sites": 42}},
    )
    assert browser_settings.browser_assisted_sites() == DEFAULT_BROWSER_ASSISTED_SITES


def test_browser_assisted_sites_forwards_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    def _load(path: str | None = None) -> dict[str, Any]:
        seen.append(path)
        return {"general": {"browser_assisted_sites": ["example"]}}

    monkeypatch.setattr(browser_settings, "load_config", _load)
    assert browser_settings.browser_assisted_sites("custom.toml") == ("example",)
    assert seen == ["custom.toml"]


def test_is_browser_assisted_site_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_settings,
        "browser_assisted_sites",
        lambda *a, **k: ("fanqienovel",),
    )
    assert browser_settings.is_browser_assisted_site(" FanqieNovel ")
    assert not browser_settings.is_browser_assisted_site("other")


# --------------------------------------------------------------------------- #
# Specs and subprocess parsing
# --------------------------------------------------------------------------- #


def test_resolve_spec_for_fanqienovel() -> None:
    spec = resolve_spec("fanqienovel")
    assert spec == BrowserSearchSpec(
        site="fanqienovel",
        home_url="https://fanqienovel.com/",
        search_url="https://fanqienovel.com/search/{keyword}",
        response_marker="search_book/v1",
    )


def test_resolve_spec_unknown_site() -> None:
    assert resolve_spec("no-such-site") is None


class _FakeProc:
    """Minimal ``Popen`` stand-in used by the subprocess tests."""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        *,
        timeout_once: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4242
        self._timeout_once = timeout_once
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self._timeout_once:
            self._timeout_once = False
            raise subprocess.TimeoutExpired(cmd="browser-search", timeout=timeout or 0)
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _enable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: True)


@pytest.fixture(autouse=True)
def _configured_browser_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent from the on-disk browser site list."""
    monkeypatch.setattr(browser_main, "is_browser_assisted_site", lambda site: True)


def _payload(
    body: str | None = None,
    *,
    error: str = "",
    used_browser: bool = True,
) -> str:
    return json.dumps(
        {
            "ok": bool(body),
            "body": body or "",
            "error": error,
            "used_browser": used_browser,
        }
    )


def test_search_via_browser_reads_json_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)
    noise = "some pywebview chatter\n"
    body = '{"code": 0}'
    stdout = f"{noise}{_payload(body)}\n"
    monkeypatch.setattr(
        browser_main.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(stdout=stdout),
    )
    assert search_via_browser("fanqienovel", "斗破苍穹") == '{"code": 0}'


def test_search_via_browser_result_reports_used_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(
        browser_main.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(stdout=_payload('{"code": 0}')),
    )
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.ok
    assert result.body == '{"code": 0}'
    assert result.used_browser is True
    assert result.elapsed >= 0


def test_search_via_browser_ignores_unusable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(
        browser_main.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(stdout="not json\n", returncode=1, stderr="boom"),
    )
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.body is None
    assert result.error is not None
    assert "no usable output" in result.error
    assert "boom" in result.error

    monkeypatch.setattr(
        browser_main.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(
            stdout=_payload(None, error="login required", used_browser=True)
        ),
    )
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.body is None
    assert result.error == "login required"


def test_search_via_browser_handles_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)

    def _boom(*a: Any, **k: Any) -> None:
        raise OSError("no python")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _boom)
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.body is None
    assert result.error is not None
    assert "could not start" in result.error
    assert result.used_browser is False


@pytest.mark.parametrize(
    "timeout",
    [0.0, -1.0, float("nan"), float("inf"), MAX_BROWSER_SEARCH_TIMEOUT + 1.0],
)
def test_search_via_browser_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    _enable_browser(monkeypatch)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("a subprocess must not be spawned")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _unexpected)

    result = search_via_browser_result("fanqienovel", "kw", timeout=timeout)

    assert result.body is None
    assert result.used_browser is False
    assert result.error is not None
    assert "timeout" in result.error


@pytest.mark.parametrize("lock_timeout", [float("nan"), float("inf"), -1.0])
def test_search_via_browser_rejects_invalid_lock_timeout(
    monkeypatch: pytest.MonkeyPatch, lock_timeout: float
) -> None:
    _enable_browser(monkeypatch)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("a subprocess must not be spawned")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _unexpected)

    result = search_via_browser_result(
        "fanqienovel", "kw", timeout=1.0, lock_timeout=lock_timeout
    )

    assert result.body is None
    assert result.error is not None
    assert "timeout" in result.error


def test_search_via_browser_kills_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)
    proc = _FakeProc(timeout_once=True)
    monkeypatch.setattr(browser_main.subprocess, "Popen", lambda *a, **k: proc)
    killed: list[_FakeProc] = []
    monkeypatch.setattr(
        browser_main, "_terminate_process_tree", lambda p: killed.append(p)
    )

    result = search_via_browser_result("fanqienovel", "kw", timeout=0.01)

    assert result.body is None
    assert result.error is not None
    assert "did not finish" in result.error
    assert result.used_browser is True
    assert killed == [proc]


def test_browser_unavailable_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: False)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("a subprocess must not be spawned")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _unexpected)
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.body is None
    assert result.error is not None
    assert "pywebview" in result.error
    assert result.used_browser is False


# --------------------------------------------------------------------------- #
# Per-site mutex
# --------------------------------------------------------------------------- #


def test_site_lock_is_shared_and_case_insensitive() -> None:
    assert browser_main._site_lock("FanqieNovel") is browser_main._site_lock(
        " fanqienovel "
    )
    assert browser_main._site_lock("fanqienovel") is not browser_main._site_lock(
        "other"
    )


def test_concurrent_search_for_same_site_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)
    lock = browser_main._site_lock("fanqienovel")
    assert lock.acquire(timeout=0)
    try:
        result = search_via_browser_result("fanqienovel", "kw", lock_timeout=0.01)
    finally:
        lock.release()

    assert result.body is None
    assert result.error is not None
    assert "already running" in result.error


def test_different_site_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(
        browser_main,
        "resolve_spec",
        lambda site: BrowserSearchSpec(
            site=site, home_url="", search_url="", response_marker=""
        ),
    )
    monkeypatch.setattr(
        browser_main.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(stdout=_payload('{"code": 0}')),
    )
    lock = browser_main._site_lock("fanqienovel")
    assert lock.acquire(timeout=0)
    try:
        result = search_via_browser_result("other-site", "kw", lock_timeout=0.01)
    finally:
        lock.release()

    assert result.ok


def test_unsupported_site_does_not_spawn_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browser(monkeypatch)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("a subprocess must not be spawned")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _unexpected)
    result = search_via_browser_result("no-such-site", "kw")
    assert result.body is None
    assert result.error is not None
    assert "does not support" in result.error


def test_unconfigured_site_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(browser_main, "is_browser_assisted_site", lambda site: False)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("a subprocess must not be spawned")

    monkeypatch.setattr(browser_main.subprocess, "Popen", _unexpected)
    result = search_via_browser_result("fanqienovel", "kw")
    assert result.body is None
    assert result.error is not None
    assert "not configured" in result.error


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def test_browser_search_main_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        browser_main,
        "run_browser_search_detailed",
        lambda *a, **k: BrowserSearchResult(body='{"code": 0}', used_browser=True),
    )
    assert browser_search_main(["fanqienovel", "斗破苍穹"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "ok": True,
        "body": '{"code": 0}',
        "error": "",
        "used_browser": True,
    }

    monkeypatch.setattr(
        browser_main,
        "run_browser_search_detailed",
        lambda *a, **k: BrowserSearchResult(error="no session", used_browser=False),
    )
    assert browser_search_main(["fanqienovel", "斗破苍穹"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "ok": False,
        "body": "",
        "error": "no session",
        "used_browser": False,
    }


def test_run_browser_search_rejects_unsupported_site() -> None:
    messages: list[str] = []
    assert run_browser_search("no-such-site", "kw", notify=messages.append) is None
    assert any("does not support" in message for message in messages)


def test_run_browser_search_detailed_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: False)
    messages: list[str] = []
    result = run_browser_search_detailed("fanqienovel", "kw", notify=messages.append)
    assert result.body is None
    assert result.error is not None
    assert "pywebview" in result.error
    assert any("pywebview" in message for message in messages)


def test_run_browser_search_reports_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: True)
    monkeypatch.setattr(browser_main, "capture_search_json", lambda *a, **k: None)
    messages: list[str] = []
    assert run_browser_search("fanqienovel", "kw", notify=messages.append) is None
    assert any("returned no data" in message for message in messages)


@pytest.mark.parametrize("timeout", [0.0, float("nan"), float("inf")])
def test_run_browser_search_detailed_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: True)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("the browser window must not be opened")

    monkeypatch.setattr(browser_main, "capture_search_json", _unexpected)
    messages: list[str] = []

    result = run_browser_search_detailed(
        "fanqienovel", "kw", timeout=timeout, notify=messages.append
    )

    assert result.body is None
    assert result.error is not None
    assert "timeout" in result.error
    assert any("timeout" in message for message in messages)


def test_capture_search_json_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare module is enough: the timeout is rejected before any webview call,
    # so the module deliberately has no usable settings/create_window.
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    spec = resolve_spec("fanqienovel")
    assert spec is not None
    messages: list[str] = []

    assert (
        capture_search_json(spec, "kw", timeout=float("inf"), notify=messages.append)
        is None
    )
    assert any("timeout" in message for message in messages)


def test_browser_search_main_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(browser_main, "is_browser_available", lambda: True)

    def _unexpected(*a: Any, **k: Any) -> None:
        raise AssertionError("the browser window must not be opened")

    monkeypatch.setattr(browser_main, "capture_search_json", _unexpected)

    assert browser_search_main(["fanqienovel", "kw", "--timeout", "nan"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert "timeout" in printed["error"]


def test_capture_search_json_without_webview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``None`` in ``sys.modules`` makes ``import webview`` raise ImportError.
    monkeypatch.setitem(sys.modules, "webview", None)
    messages: list[str] = []
    spec = resolve_spec("fanqienovel")
    assert spec is not None
    assert capture_search_json(spec, "kw", notify=messages.append) is None
    assert any("pywebview is required" in message for message in messages)


# --------------------------------------------------------------------------- #
# Search orchestration observability
# --------------------------------------------------------------------------- #

_CALLS: list[bool] = []


class _BrowserSiteSearcher(BaseSearcher):
    """Fake browser-assisted site that records explicit selection."""

    site_name = "fanqienovel"

    async def _fetch_html(self, keyword: str) -> str:
        _CALLS.append(self.explicitly_selected)
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            SearchResult(
                site=self.site_name,
                book_id="1",
                book_url="https://example.invalid/1",
                cover_url="",
                title="title",
                author="author",
                latest_chapter="-",
                update_date="-",
                word_count="-",
                priority=1,
            )
        ]


@pytest.fixture(autouse=True)
def _isolated_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep orchestration tests independent of the on-disk config and cache."""
    _CALLS.clear()
    search_module._CACHE.clear()

    def _classes(
        sites: Any = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return [_BrowserSiteSearcher]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    monkeypatch.setattr(
        search_module, "_browser_assisted_sites", lambda: frozenset({"fanqienovel"})
    )


def test_aggregate_search_skips_browser_assisted_site() -> None:
    events: list[search_module.SearchEvent] = []
    results = asyncio.run(search_module.search("kw", on_event=events.append))

    assert results == []
    assert _CALLS == []
    assert [(e.site, e.kind) for e in events] == [("fanqienovel", "browser_skipped")]


def test_explicit_search_enables_browser_assistance() -> None:
    events: list[search_module.SearchEvent] = []
    results = asyncio.run(
        search_module.search("kw", sites=["fanqienovel"], on_event=events.append)
    )

    assert len(results) == 1
    assert _CALLS == [True]
    assert ("fanqienovel", "browser_assisted") in [(e.site, e.kind) for e in events]
    completed = [e for e in events if e.kind == "completed"]
    assert len(completed) == 1
    assert completed[0].result_count == 1


def test_search_stream_keeps_chunk_contract_and_reports_events() -> None:
    async def _run() -> tuple[list[list[SearchResult]], list[Any]]:
        events: list[Any] = []
        chunks = [
            chunk
            async for chunk in search_module.search_stream(
                "kw", sites=["fanqienovel"], on_event=events.append
            )
        ]
        return chunks, events

    chunks, events = asyncio.run(_run())

    assert len(chunks) == 1
    assert chunks[0][0]["book_id"] == "1"
    assert [e.kind for e in events] == ["browser_assisted", "completed"]


def test_search_stream_aggregate_skips_without_yielding() -> None:
    async def _run() -> tuple[list[list[SearchResult]], list[Any]]:
        events: list[Any] = []
        chunks = [
            chunk
            async for chunk in search_module.search_stream("kw", on_event=events.append)
        ]
        return chunks, events

    chunks, events = asyncio.run(_run())

    assert chunks == []
    assert _CALLS == []
    assert [e.kind for e in events] == ["browser_skipped"]


def test_event_callback_errors_do_not_break_search() -> None:
    def _boom(event: Any) -> None:
        raise RuntimeError("observer failed")

    results = asyncio.run(
        search_module.search("kw", sites=["fanqienovel"], on_event=_boom)
    )
    assert len(results) == 1


class _BrokenBrowserSearcher(BaseSearcher):
    site_name = "fanqienovel"

    async def _fetch_html(self, keyword: str) -> str:
        raise RuntimeError("browser search exploded")

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


def test_search_reports_failed_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        lambda sites=None, *, load_all_if_none=True: [_BrokenBrowserSearcher],
    )
    events: list[search_module.SearchEvent] = []
    results = asyncio.run(
        search_module.search("kw", sites=["fanqienovel"], on_event=events.append)
    )

    assert results == []
    assert [e.kind for e in events] == ["browser_assisted", "failed"]
    assert "exploded" in events[-1].message


# --------------------------------------------------------------------------- #
# Timeout budget and browser profile hardening
# --------------------------------------------------------------------------- #


class _FakeLoadedEvent:
    def __init__(self, loaded: bool) -> None:
        self._loaded = loaded
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self._loaded


class _FakeWindow:
    def __init__(self, *, loaded: bool) -> None:
        self.events = types.SimpleNamespace(loaded=_FakeLoadedEvent(loaded))
        self.destroyed = False
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)

    def destroy(self) -> None:
        self.destroyed = True


def _install_fake_webview(
    monkeypatch: pytest.MonkeyPatch, window: _FakeWindow, profile_dir
) -> None:
    module = types.ModuleType("webview")
    module.settings = {}  # type: ignore[attr-defined]
    module.create_window = lambda **kwargs: window  # type: ignore[attr-defined]
    module.start = lambda func, **kwargs: func()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", module)
    # Keep the persistent profile out of the real cache directory.
    monkeypatch.setattr(browser_main, "_private_profile_dir", lambda site: profile_dir)


def test_capture_search_json_bounds_home_page_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    window = _FakeWindow(loaded=False)
    _install_fake_webview(monkeypatch, window, tmp_path / "profile")
    spec = resolve_spec("fanqienovel")
    assert spec is not None

    assert (
        capture_search_json(spec, "kw", timeout=0.5, notify=lambda message: None)
        is None
    )

    assert window.destroyed
    assert len(window.events.loaded.waits) == 1
    waited = window.events.loaded.waits[0]
    assert waited is not None
    assert 0.0 <= waited <= 0.6


def test_capture_search_json_bounds_devtools_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    window = _FakeWindow(loaded=True)
    _install_fake_webview(monkeypatch, window, tmp_path / "profile")
    spec = resolve_spec("fanqienovel")
    assert spec is not None

    waits: list[float | None] = []

    class _RecordingEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            return super().wait(timeout)

    # Replace only the module's threading reference, so events created inside
    # asyncio's machinery are not recorded as the DevTools wait.
    monkeypatch.setattr(
        browser_main,
        "threading",
        types.SimpleNamespace(Event=_RecordingEvent, Thread=threading.Thread),
    )

    async def _capture(port, marker, deadline, result, ready) -> None:  # type: ignore[no-untyped-def]
        result["body"] = '{"ok": 1}'
        result["done"] = True
        ready.set()

    monkeypatch.setattr(browser_main, "_capture_search_response", _capture)

    body = capture_search_json(spec, "kw", timeout=1.5, notify=lambda message: None)

    assert body == '{"ok": 1}'
    assert waits
    waited = waits[0]
    assert waited is not None
    assert 0.0 <= waited <= 1.6


def test_private_profile_dir_is_created_owner_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cache" / "profile"
    monkeypatch.setattr(browser_main, "_profile_dir", lambda site: target)

    created = browser_main._private_profile_dir("fanqienovel")

    assert created == target
    assert target.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no POSIX permission bits"
)
def test_private_profile_dir_tightens_existing_permissions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cache" / "profile"
    target.mkdir(parents=True, mode=0o755)
    os.chmod(target, 0o755)
    monkeypatch.setattr(browser_main, "_profile_dir", lambda site: target)

    browser_main._private_profile_dir("fanqienovel")

    assert stat.S_IMODE(target.stat().st_mode) == 0o700
