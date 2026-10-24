#!/usr/bin/env python3
"""
tests.apps.test_login_flow
--------------------------

Offline tests for the interactive login flow.

No real browser window is opened and no network request is made: ``webview``
and ``urllib.request.urlopen`` are monkeypatched, and the timing helpers are
exercised with explicit timestamps.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
import urllib.error
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.login import main as login
from novel_downloader.apps.login.sites import (
    LOGIN_SITE_CONFIGS,
    SiteLoginConfig,
    evaluate_probe_payload,
    get_site_config,
    login_sites,
    normalize_site,
    supported_sites,
)

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Context-manager stand-in for ``urllib.request.urlopen`` responses."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _Event:
    """
    Minimal pywebview event stand-in.

    ``close_after`` makes ``wait`` report the event as set on the Nth call,
    which is used to simulate the user closing the window.
    """

    def __init__(self, *, set_: bool = False, close_after: int | None = None) -> None:
        self._set = set_
        self._close_after = close_after
        self.wait_calls = 0
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls += 1
        if timeout is not None:
            self.waits.append(timeout)
        if self._close_after is not None and self.wait_calls >= self._close_after:
            self._set = True
        return self._set


class _FakeWindow:
    """Minimal pywebview window stand-in."""

    def __init__(
        self,
        cookies: list[dict[str, Any]] | None = None,
        *,
        loaded: _Event | None = None,
        closed: _Event | None = None,
        url: str = "",
        cookies_sequence: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self._cookies = cookies or []
        self._cookies_sequence = cookies_sequence
        self._polls = 0
        self.destroyed = False
        self.url = url
        self.events = types.SimpleNamespace(
            loaded=loaded if loaded is not None else _Event(set_=True),
            closed=closed if closed is not None else _Event(),
        )

    def get_cookies(self) -> list[dict[str, Any]]:
        if self._cookies_sequence is not None:
            index = min(self._polls, len(self._cookies_sequence) - 1)
            self._polls += 1
            return self._cookies_sequence[index]
        return self._cookies

    def get_current_url(self) -> str:
        return self.url

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWebview:
    """Stand-in for the ``webview`` module."""

    def __init__(
        self,
        window: _FakeWindow | None = None,
        *,
        create_error: Exception | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self._window = window
        self._create_error = create_error
        self._start_error = start_error
        self.started = False
        self.create_kwargs: dict[str, Any] = {}

    def create_window(self, **kwargs: Any) -> _FakeWindow | None:
        self.create_kwargs = kwargs
        if self._create_error is not None:
            raise self._create_error
        return self._window

    def start(self, func: Any = None, *args: Any, **kwargs: Any) -> None:
        self.started = True
        if self._start_error is not None:
            raise self._start_error
        if callable(func):
            func()


def _install_webview(monkeypatch: pytest.MonkeyPatch, fake: _FakeWebview) -> None:
    monkeypatch.setitem(sys.modules, "webview", fake)


# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------


def test_site_config_table_is_consistent() -> None:
    assert LOGIN_SITE_CONFIGS
    for key, config in LOGIN_SITE_CONFIGS.items():
        assert key == config.site
        assert key == normalize_site(key)
        assert config.start_url.startswith("https://")
        if config.probe_url is not None:
            assert config.probe_url.startswith("https://")
        assert config.probe_kind in {"cookies", "json_code", "text_markers"}
        assert all(config.required_cookies)
        assert all(config.cookie_names)


def test_login_sites_derived_from_config_table() -> None:
    expected = {site: config.start_url for site, config in LOGIN_SITE_CONFIGS.items()}
    assert login_sites() == expected
    assert expected == login.LOGIN_SITES
    assert supported_sites() == tuple(sorted(expected))


def test_get_site_config_normalizes_key() -> None:
    config = get_site_config("  FanQieNovel  ")
    assert config is not None
    assert config.site == "fanqienovel"


def test_get_site_config_unknown_returns_none() -> None:
    assert get_site_config("does-not-exist") is None


def test_fanqienovel_probe_rule_matches_implementation() -> None:
    config = get_site_config("fanqienovel")
    assert config is not None
    assert config.probe_url == "https://fanqienovel.com/api/user/info/v2"
    assert config.probe_kind == "json_code"
    assert config.json_success_code == 0
    assert config.needs_probe is True


def test_cookie_only_site_url_and_cookie_rules() -> None:
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        required_cookies=("sid",),
        cookie_names=("sid", "theme"),
        login_url_keywords=("/login", "/signin"),
        success_url_keywords=("/account",),
        probe_kind="cookies",
    )

    assert config.needs_probe is False
    assert config.has_required_cookies({"sid": "x"}) is True
    assert config.has_required_cookies({"other": "x"}) is False
    assert config.filter_cookies({"sid": "x", "theme": "dark", "tracking": "1"}) == {
        "sid": "x",
        "theme": "dark",
    }
    assert config.is_login_url("https://example.com/SIGNIN") is True
    assert config.is_login_url("https://example.com/account") is False
    assert config.is_success_url("https://example.com/account/settings") is True
    assert config.is_success_url("") is False


def test_filter_cookies_without_allow_list_keeps_everything() -> None:
    config = SiteLoginConfig(site="example", start_url="https://example.com/")
    cookies = {"a": "1", "b": "2"}
    assert config.filter_cookies(cookies) == cookies


# ---------------------------------------------------------------------------
# Probe interpretation (pure)
# ---------------------------------------------------------------------------


def test_evaluate_json_code_probe() -> None:
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        probe_url="https://example.com/probe",
        probe_kind="json_code",
    )

    assert evaluate_probe_payload(config, b'{"code": 0}') is True
    assert evaluate_probe_payload(config, '{"code": "0"}') is True
    assert evaluate_probe_payload(config, b'{"code": 1}') is False
    assert evaluate_probe_payload(config, b'{"code": "denied"}') is False
    assert evaluate_probe_payload(config, b'{"code": true}') is False
    assert evaluate_probe_payload(config, b'{"code": 0.5}') is False
    assert evaluate_probe_payload(config, b'{"code": null}') is False
    assert evaluate_probe_payload(config, b"[0]") is False
    assert evaluate_probe_payload(config, b"not json") is False
    assert evaluate_probe_payload(config, b"") is False
    assert evaluate_probe_payload(config, None) is False


def test_evaluate_json_code_probe_honours_custom_field() -> None:
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        probe_kind="json_code",
        json_code_field="status",
        json_success_code=200,
    )
    assert config.evaluate_probe(b'{"status": 200}') is True
    assert config.evaluate_probe(b'{"status": 401}') is False


def test_evaluate_text_marker_probe() -> None:
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        probe_kind="text_markers",
        anonymous_markers=("Sign in", "登录"),
        success_markers=("My account",),
    )

    assert config.evaluate_probe("Welcome! My account") is True
    # Anonymous markers win even when a success marker is present.
    assert config.evaluate_probe("My account - Sign in") is False
    assert config.evaluate_probe("欢迎，请登录") is False
    assert config.evaluate_probe("Something else") is False
    assert config.evaluate_probe("   ") is False


def test_evaluate_text_marker_probe_without_success_markers() -> None:
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        probe_kind="text_markers",
        anonymous_markers=("login",),
    )
    assert config.evaluate_probe("dashboard") is True
    assert config.evaluate_probe("please Login") is False


def test_evaluate_cookie_mode_always_true() -> None:
    config = SiteLoginConfig(site="example", start_url="https://example.com/")
    assert config.evaluate_probe(None) is True


# ---------------------------------------------------------------------------
# Cookie extraction
# ---------------------------------------------------------------------------


def test_collect_cookies_from_backend_dicts() -> None:
    window = _FakeWindow(
        cookies=[
            {
                "name": "sessionid",
                "value": "secret",
                "domain": ".fanqienovel.com",
                "path": "/",
            },
            {"name": "t", "value": 123},
        ]
    )
    assert login._collect_cookies(window) == {"sessionid": "secret", "t": "123"}


def test_collect_cookies_from_simple_cookie() -> None:
    jar: SimpleCookie[str] = SimpleCookie()
    jar["sessionid"] = "secret"
    jar["t"] = "1"

    window = types.SimpleNamespace(get_cookies=lambda: [jar])
    assert login._collect_cookies(window) == {"sessionid": "secret", "t": "1"}


def test_collect_cookies_drops_invalid_names_and_control_characters() -> None:
    window = _FakeWindow(
        cookies=[
            {"name": "good", "value": "a\x00b"},
            {"name": "bad name", "value": "x"},
            {"name": "", "value": "x"},
            {"value": "missing-name"},
        ]
    )
    assert login._collect_cookies(window) == {"good": "ab"}


@pytest.mark.parametrize("raw", [None, [], ()])
def test_collect_cookies_handles_empty_payloads(raw: Any) -> None:
    window = types.SimpleNamespace(get_cookies=lambda: raw)
    assert login._collect_cookies(window) == {}


def test_collect_cookies_tolerates_failures() -> None:
    def boom() -> list[Any]:
        raise RuntimeError("browser gone")

    assert login._collect_cookies(types.SimpleNamespace(get_cookies=boom)) == {}
    assert login._collect_cookies(object()) == {}


def test_cookie_pairs_rejects_malformed_items() -> None:
    assert login._cookie_pairs({"name": "ok", "value": "1"}) == {"ok": "1"}
    assert login._cookie_pairs({"name": "ok"}) == {"ok": ""}
    assert login._cookie_pairs({"value": "1"}) == {}
    assert login._cookie_pairs(42) == {}


# ---------------------------------------------------------------------------
# Cookie persistence
# ---------------------------------------------------------------------------


def test_save_cookies_writes_atomically_and_leaves_no_temp_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))

    path = login.save_cookies("fanqienovel", {"sessionid": "abc", "t": "1"})

    assert path == tmp_path / "fanqienovel" / login.COOKIE_FILENAME
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"name": "sessionid", "value": "abc"},
        {"name": "t", "value": "1"},
    ]
    leftovers = [
        child.name
        for child in path.parent.iterdir()
        if child.name != login.COOKIE_FILENAME
    ]
    assert leftovers == []


def test_save_cookies_drops_invalid_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))

    path = login.save_cookies(
        "fanqienovel",
        {"good": "value\x01", "bad name": "x", "": "empty"},
    )

    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"name": "good", "value": "value"}
    ]


def test_save_cookies_rejects_cache_dir_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A site key becomes a directory name, so separators and ``..`` must be
    # rejected instead of escaping the cache directory.
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        login.save_cookies("../escape", {"a": "1"})
    assert not (tmp_path.parent / "escape").exists()


# ---------------------------------------------------------------------------
# Timeout / polling helpers
# ---------------------------------------------------------------------------


def test_remaining_is_never_negative() -> None:
    assert login._remaining(10.0, now=9.5) == pytest.approx(0.5)
    assert login._remaining(10.0, now=10.0) == 0.0
    assert login._remaining(10.0, now=99.0) == 0.0


def test_poll_delay_clamps_to_interval_and_deadline() -> None:
    assert login._poll_delay(10.0, 1.5, now=0.0) == pytest.approx(1.5)
    assert login._poll_delay(10.0, 1.5, now=9.5) == pytest.approx(0.5)
    assert login._poll_delay(10.0, 1.5, now=10.0) is None
    assert login._poll_delay(10.0, 1.5, now=11.0) is None


def test_wait_for_event_returns_immediately_when_set() -> None:
    assert login._wait_for_event(_Event(set_=True), _Event(), 5.0) == "event"


def test_wait_for_event_detects_closed_window() -> None:
    assert login._wait_for_event(_Event(), _Event(set_=True), 5.0) == "closed"


def test_wait_for_event_times_out_without_blocking() -> None:
    assert login._wait_for_event(_Event(), _Event(), 0.0) == "timeout"
    assert login._wait_for_event(None, _Event(), 5.0) == "timeout"


def test_wait_for_event_accepts_wait_result() -> None:
    class _WaitOnly:
        def wait(self, timeout: float | None = None) -> bool:
            return True

    assert login._wait_for_event(_WaitOnly(), _Event(), 5.0) == "event"


def test_wait_for_event_falls_back_when_wait_takes_no_timeout() -> None:
    class _NoTimeout:
        def is_set(self) -> bool:
            return False

        def wait(self) -> bool:
            raise TypeError("no timeout argument")

    # The fallback sleeps in small slices; a zero timeout avoids a real sleep.
    assert login._wait_for_event(_NoTimeout(), _Event(), 0.0) == "timeout"


# ---------------------------------------------------------------------------
# Login verification (network monkeypatched)
# ---------------------------------------------------------------------------


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("network must not be touched")


def test_is_logged_in_short_circuits_without_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    config = get_site_config("fanqienovel")
    assert config is not None
    assert login.is_logged_in(config, {}) is False


def test_is_logged_in_short_circuits_on_missing_required_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    config = SiteLoginConfig(
        site="example",
        start_url="https://example.com/",
        required_cookies=("sid",),
        probe_url="https://example.com/probe",
        probe_kind="json_code",
    )
    assert login.is_logged_in(config, {"other": "1"}) is False


def test_is_logged_in_probe_success_and_request_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["cookie"] = request.get_header("Cookie")
        captured["referer"] = request.get_header("Referer")
        captured["ua"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return _FakeResponse(b'{"code": 0}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = get_site_config("fanqienovel")
    assert config is not None

    assert login.is_logged_in(config, {"sessionid": "secret"}, timeout=2.5) is True
    assert captured["url"] == "https://fanqienovel.com/api/user/info/v2"
    assert captured["cookie"] == "sessionid=secret"
    assert captured["referer"] == "https://fanqienovel.com/"
    assert "Mozilla" in captured["ua"]
    assert captured["timeout"] == pytest.approx(2.5)


def test_is_logged_in_probe_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _FakeResponse(b'{"code": 100}'),
    )
    config = get_site_config("fanqienovel")
    assert config is not None
    assert login.is_logged_in(config, {"sessionid": "secret"}) is False


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("offline"),
        OSError("socket closed"),
        TimeoutError("slow"),
        ValueError("bad value"),
    ],
)
def test_is_logged_in_treats_probe_errors_as_not_logged_in(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(request: Any, timeout: float) -> Any:
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    config = get_site_config("fanqienovel")
    assert config is not None
    assert login.is_logged_in(config, {"sessionid": "secret"}) is False


def test_is_logged_in_without_probe_accepts_cookies() -> None:
    config = get_site_config("fanqienovel")
    assert config is not None
    no_probe = login.dataclasses.replace(config, probe_url=None)
    assert login.is_logged_in(no_probe, {"a": "1"}) is True
    assert login.is_logged_in(no_probe, {}) is False


def test_looks_logged_in_unknown_site_keeps_legacy_behaviour() -> None:
    assert login._looks_logged_in("unknown-site", {"a": "1"}) is True
    assert login._looks_logged_in("unknown-site", {}) is False


# ---------------------------------------------------------------------------
# run_login_result: window lifecycle and error mapping
# ---------------------------------------------------------------------------


def test_run_login_result_saves_cookies_and_destroys_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})

    window = _FakeWindow(
        cookies=[{"name": "sessionid", "value": "secret", "domain": ".fanqienovel.com"}]
    )
    fake = _FakeWebview(window)
    _install_webview(monkeypatch, fake)

    outcome = login.run_login_result(
        "fanqienovel", timeout=30.0, notify=lambda _msg: None
    )

    assert outcome.ok is True
    assert outcome.status is login.LoginStatus.SUCCESS
    assert outcome.site == "fanqienovel"
    assert outcome.path == tmp_path / "fanqienovel" / login.COOKIE_FILENAME
    assert json.loads(outcome.path.read_text(encoding="utf-8")) == [
        {"name": "sessionid", "value": "secret"}
    ]
    assert fake.started is True
    assert fake.create_kwargs["url"] == "https://fanqienovel.com/"
    assert fake.create_kwargs["width"] == 1000
    assert fake.create_kwargs["height"] == 760
    assert window.destroyed is True


def test_run_login_result_unsupported_site() -> None:
    messages: list[str] = []
    outcome = login.run_login_result("nope", notify=messages.append)

    assert outcome.status is login.LoginStatus.UNSUPPORTED_SITE
    assert outcome.site == "nope"
    assert messages and "nope" in messages[0]
    assert login.exit_code_for(outcome.status) == login.EXIT_LOGIN_USAGE


def test_run_login_result_missing_pywebview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``None`` in sys.modules makes ``import webview`` raise ImportError even
    # when pywebview is installed in the test environment.
    monkeypatch.setitem(sys.modules, "webview", None)
    messages: list[str] = []

    outcome = login.run_login_result("fanqienovel", notify=messages.append)

    assert outcome.status is login.LoginStatus.WEBVIEW_UNAVAILABLE
    assert "pywebview" in (outcome.error or "")
    assert messages and "pywebview" in messages[-1]
    assert login.exit_code_for(outcome.status) == login.EXIT_LOGIN_UNAVAILABLE


def test_run_login_result_create_window_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWebview(create_error=RuntimeError("no display available"))
    _install_webview(monkeypatch, fake)
    messages: list[str] = []

    outcome = login.run_login_result("fanqienovel", notify=messages.append)

    assert outcome.status is login.LoginStatus.WINDOW_ERROR
    assert "no display available" in (outcome.error or "")
    assert any("no display available" in message for message in messages)
    assert login.exit_code_for(outcome.status) == login.EXIT_LOGIN_UNAVAILABLE


def test_run_login_result_create_window_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_webview(monkeypatch, _FakeWebview(window=None))
    outcome = login.run_login_result("fanqienovel", notify=lambda _msg: None)

    assert outcome.status is login.LoginStatus.WINDOW_ERROR
    assert outcome.error == "create_window returned None"


def test_run_login_result_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWebview(
        _FakeWindow(), start_error=RuntimeError("GUI backend unavailable")
    )
    _install_webview(monkeypatch, fake)
    messages: list[str] = []

    outcome = login.run_login_result("fanqienovel", notify=messages.append)

    assert outcome.status is login.LoginStatus.WINDOW_ERROR
    assert "GUI backend unavailable" in (outcome.error or "")
    assert any("GUI backend unavailable" in message for message in messages)


def test_run_login_result_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    window = _FakeWindow()
    _install_webview(monkeypatch, _FakeWebview(window))
    messages: list[str] = []

    outcome = login.run_login_result("fanqienovel", timeout=0.0, notify=messages.append)

    assert outcome.status is login.LoginStatus.TIMEOUT
    assert any("0 seconds" in message for message in messages)
    assert window.destroyed is True


def test_run_login_result_cancelled_when_window_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    closed = _Event(close_after=1)
    window = _FakeWindow(closed=closed)
    _install_webview(monkeypatch, _FakeWebview(window))
    messages: list[str] = []

    outcome = login.run_login_result(
        "fanqienovel",
        timeout=30.0,
        poll_interval=0.1,
        notify=messages.append,
    )

    assert outcome.status is login.LoginStatus.CANCELLED
    assert closed.waits == [pytest.approx(0.1)]
    assert any("closed" in message for message in messages)
    assert not (tmp_path / "fanqienovel" / login.COOKIE_FILENAME).exists()
    assert window.destroyed is True


def test_run_login_result_cancelled_before_page_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _Event()
    closed = _Event(set_=True)
    window = _FakeWindow(loaded=loaded, closed=closed)
    _install_webview(monkeypatch, _FakeWebview(window))

    outcome = login.run_login_result(
        "fanqienovel", timeout=30.0, notify=lambda _msg: None
    )

    assert outcome.status is login.LoginStatus.CANCELLED
    assert window.destroyed is True


def test_run_login_result_polls_until_cookies_appear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    sequence = [[], [], [{"name": "sessionid", "value": "late"}]]
    window = _FakeWindow(cookies_sequence=sequence, closed=_Event())
    _install_webview(monkeypatch, _FakeWebview(window))

    outcome = login.run_login_result(
        "fanqienovel",
        timeout=30.0,
        poll_interval=0.1,
        notify=lambda _msg: None,
    )

    assert outcome.status is login.LoginStatus.SUCCESS
    assert window._polls >= 3


def test_run_login_result_save_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    window = _FakeWindow(cookies=[{"name": "sessionid", "value": "secret"}])
    _install_webview(monkeypatch, _FakeWebview(window))

    def fail_save(site: str, cookies: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(login, "save_cookies", fail_save)
    messages: list[str] = []

    outcome = login.run_login_result(
        "fanqienovel", timeout=30.0, notify=messages.append
    )

    assert outcome.status is login.LoginStatus.SAVE_ERROR
    assert "disk full" in (outcome.error or "")
    assert any("disk full" in message for message in messages)
    assert window.destroyed is True


def test_run_login_result_watcher_exception_becomes_window_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _FakeWindow()
    _install_webview(monkeypatch, _FakeWebview(window))

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected watcher failure")

    monkeypatch.setattr(login, "_watch_window", explode)
    messages: list[str] = []

    outcome = login.run_login_result("fanqienovel", notify=messages.append)

    assert outcome.status is login.LoginStatus.WINDOW_ERROR
    assert "unexpected watcher failure" in (outcome.error or "")
    assert any("unexpected watcher failure" in message for message in messages)
    assert window.destroyed is True


def test_run_login_result_notify_errors_do_not_break_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    window = _FakeWindow(cookies=[{"name": "sessionid", "value": "secret"}])
    _install_webview(monkeypatch, _FakeWebview(window))

    def bad_notify(message: str) -> None:
        raise RuntimeError("sink exploded")

    outcome = login.run_login_result("fanqienovel", notify=bad_notify)
    assert outcome.status is login.LoginStatus.SUCCESS


# ---------------------------------------------------------------------------
# Config overrides and CLI mapping
# ---------------------------------------------------------------------------


def test_site_config_overrides_honour_login_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        login, "_LOGIN_PROBES", {"fanqienovel": "https://example.com/probe"}
    )
    config = login._site_config("fanqienovel")
    assert config is not None
    assert config.probe_url == "https://example.com/probe"

    monkeypatch.setattr(login, "_LOGIN_PROBES", {})
    config = login._site_config("fanqienovel")
    assert config is not None
    assert config.probe_url is None
    assert config.needs_probe is False


def test_site_config_supports_dynamically_added_login_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(login.LOGIN_SITES, "newsite", "https://newsite.example/")
    monkeypatch.setitem(
        login._LOGIN_PROBES, "newsite", "https://newsite.example/api/me"
    )

    config = login._site_config(" Newsite ")
    assert config is not None
    assert config.site == "newsite"
    assert config.start_url == "https://newsite.example/"
    assert config.probe_url == "https://newsite.example/api/me"
    assert config.probe_kind == "json_code"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (login.LoginStatus.SUCCESS, 0),
        (login.LoginStatus.UNSUPPORTED_SITE, 2),
        (login.LoginStatus.WEBVIEW_UNAVAILABLE, 3),
        (login.LoginStatus.WINDOW_ERROR, 3),
        (login.LoginStatus.TIMEOUT, 1),
        (login.LoginStatus.CANCELLED, 1),
        (login.LoginStatus.SAVE_ERROR, 1),
    ],
)
def test_exit_code_mapping(status: login.LoginStatus, expected: int) -> None:
    assert login.exit_code_for(status) == expected


def test_login_main_maps_outcomes_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        login,
        "run_login_result",
        lambda site, **kwargs: login.LoginOutcome(
            login.LoginStatus.SUCCESS, site, path=Path("x")
        ),
    )
    assert login.login_main(["fanqienovel"]) == 0

    monkeypatch.setattr(
        login,
        "run_login_result",
        lambda site, **kwargs: login.LoginOutcome(login.LoginStatus.TIMEOUT, site),
    )
    assert login.login_main(["fanqienovel"]) == 1

    monkeypatch.setattr(
        login,
        "run_login_result",
        lambda site, **kwargs: login.LoginOutcome(
            login.LoginStatus.UNSUPPORTED_SITE, site
        ),
    )
    assert login.login_main(["nope"]) == 2

    monkeypatch.setattr(
        login,
        "run_login_result",
        lambda site, **kwargs: login.LoginOutcome(
            login.LoginStatus.WEBVIEW_UNAVAILABLE, site
        ),
    )
    assert login.login_main(["fanqienovel"]) == 3


def test_login_main_rejects_non_positive_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("login must not start")

    monkeypatch.setattr(login, "run_login_result", must_not_run)
    assert login.login_main(["fanqienovel", "--timeout", "0"]) == 2
    assert "greater than zero" in capsys.readouterr().err


def test_login_main_uses_the_documented_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    def fake_run(site: str, *, timeout: float) -> login.LoginOutcome:
        captured["timeout"] = timeout
        return login.LoginOutcome(login.LoginStatus.SUCCESS, site)

    monkeypatch.setattr(login, "run_login_result", fake_run)
    assert login.login_main([]) == 0
    assert captured["timeout"] == pytest.approx(300.0)

    assert login.login_main(["fanqienovel", "--timeout", "42.5"]) == 0
    assert captured["timeout"] == pytest.approx(42.5)


def test_login_main_lists_only_supported_sites(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert login.login_main(["--list"]) == 0
    out = capsys.readouterr().out.split()
    assert out == sorted(LOGIN_SITE_CONFIGS)


def test_package_module_entrypoint_lists_sites() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "novel_downloader.apps.login", "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == sorted(LOGIN_SITE_CONFIGS)
