#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.apps.music.browser_login`.

Every side effect (HTTP calls to the CDP endpoint, websocket traffic,
subprocess launches) is replaced by in-memory doubles, so no real browser is
ever started and nothing outside ``tmp_path`` is touched.

The target module is deliberately untyped (see the ``[tool.mypy.overrides]``
relaxation in ``pyproject.toml``), so calls into it skip that check here too.
"""

# mypy: disallow-untyped-calls=False

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from novel_downloader.apps.music import browser_login as bl
from novel_downloader.apps.music.browser_login import (
    BrowserLogin,
    BrowserLoginError,
    find_browser,
)

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Replaces the module-level ``requests`` object."""

    def __init__(
        self,
        version: dict[str, Any] | None = None,
        new_target: dict[str, Any] | None = None,
        put_status: int = 200,
    ) -> None:
        self.version = version
        self.new_target = new_target or {"id": "T1", "webSocketDebuggerUrl": "ws://x"}
        self.put_status = put_status
        self.get_calls: list[str] = []
        self.put_calls: list[str] = []

    def get(self, url: str, timeout: float = 0) -> FakeResponse:
        self.get_calls.append(url)
        if "/json/version" in url:
            return FakeResponse(200, self.version if self.version is not None else {})
        if "/json/new" in url:
            return FakeResponse(200, self.new_target)
        return FakeResponse(200, {})

    def put(self, url: str, timeout: float = 0) -> FakeResponse:
        self.put_calls.append(url)
        return FakeResponse(self.put_status, self.new_target)


class FakeWs:
    """Replaces ``websocket.create_connection``.

    With no canned responses it echoes the id of the last request, which is
    what ``_send`` waits for.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._responses = list(responses or [])
        self._last_id = 0

    def send(self, payload: str) -> None:
        self.sent.append(payload)
        self._last_id = json.loads(payload).get("id", self._last_id)

    def recv(self) -> str:
        if self._responses:
            return json.dumps(self._responses.pop(0))
        return json.dumps({"id": self._last_id, "result": {}})

    def close(self) -> None:
        self.closed = True


def _fake_time(start: float = 1000.0, step: float = 5.0) -> Any:
    class _Clock:
        def __init__(self) -> None:
            self.now = start
            self.sleeps: list[float] = []

        def time(self) -> float:
            self.now += step
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

    return _Clock()


# --------------------------------------------------------------------------- #
# find_browser
# --------------------------------------------------------------------------- #


def test_find_browser_returns_first_existing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "msedge.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(bl, "_CANDIDATES", [str(tmp_path / "missing.exe"), str(exe)])
    assert find_browser() == str(exe)


def test_find_browser_raises_when_none_exists(tmp_path: Any) -> None:
    import novel_downloader.apps.music.browser_login as bl_mod

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(bl_mod, "_CANDIDATES", [str(tmp_path / "nope.exe")])
        with pytest.raises(BrowserLoginError, match="Edge 或 Chrome"):
            find_browser()
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# start()
# --------------------------------------------------------------------------- #


def test_start_reuses_existing_debug_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(version={"Browser": "Chrome/120"})
    monkeypatch.setattr(bl, "requests", http)
    login = BrowserLogin(profile_dir="unused")
    assert login.start() == "Chrome/120"
    assert login.launched is False


def test_start_launches_browser_and_polls_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(bl, "_CANDIDATES", [str(exe)])

    launched: list[list[str]] = []

    class _PopenStub:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            launched.append(cmd)

    monkeypatch.setattr(bl, "subprocess", type("S", (), {"Popen": _PopenStub}))
    monkeypatch.setattr(bl, "time", _fake_time())

    login = BrowserLogin(profile_dir=str(tmp_path / "profile"))
    # Patch _version after construction to control the poll sequence.
    results = iter([None, None, {"Browser": "Edge/1"}])
    monkeypatch.setattr(
        login,
        "_version",
        lambda: next(results),
    )

    assert login.start() == "Edge/1"
    assert login.launched is True
    assert len(launched) == 1
    cmd = launched[0]
    assert any("--remote-debugging-port=9333" in a for a in cmd)
    assert any(str(tmp_path / "profile") in a for a in cmd)


def test_start_timeout_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(bl, "_CANDIDATES", [str(exe)])

    class _PopenStub:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(bl, "subprocess", type("S", (), {"Popen": _PopenStub}))
    clock = _fake_time()
    monkeypatch.setattr(bl, "time", clock)

    login = BrowserLogin(profile_dir=str(tmp_path / "profile"))
    monkeypatch.setattr(login, "_version", lambda: None)

    with pytest.raises(BrowserLoginError, match="超时"):
        login.start(timeout=2)


# --------------------------------------------------------------------------- #
# CDP plumbing
# --------------------------------------------------------------------------- #


def test_connect_uses_put_then_get_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(put_status=500)
    monkeypatch.setattr(bl, "requests", http)

    ws = FakeWs()
    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.websocket.create_connection",
        lambda *a, **k: ws,
    )

    login = BrowserLogin(profile_dir="p")
    target = login._connect("https://music.163.com/")

    assert target["id"] == "T1"
    assert len(http.put_calls) == 1
    assert len(http.get_calls) == 1
    assert login._target_id == "T1"
    assert "Network.enable" in ws.sent[0]
    assert "Page.enable" in ws.sent[1]


def test_send_raises_on_cdp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWs(responses=[{"id": 1, "error": {"message": "bad method"}}])
    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.websocket.create_connection",
        lambda *a, **k: ws,
    )
    login = BrowserLogin(profile_dir="p")
    login.ws = ws
    with pytest.raises(BrowserLoginError, match="Network.enable 失败"):
        login._send("Network.enable")


def test_send_skips_unrelated_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWs(
        responses=[
            {"id": 100, "result": {}},  # unrelated event first
            {"id": 1, "result": {"cookies": []}},
        ]
    )
    login = BrowserLogin(profile_dir="p")
    login.ws = ws
    assert login._send("Network.getCookies") == {"cookies": []}


def test_read_cookies_filters_empty_entries() -> None:
    login = BrowserLogin(profile_dir="p")
    sent: list[tuple[str, dict[str, Any]]] = []

    def _fake_send(method: str, **params: Any) -> Any:
        sent.append((method, params))
        return {
            "cookies": [
                {"name": "MUSIC_U", "value": "u1"},
                {"name": "", "value": "ignored"},
                {"name": "empty_val", "value": ""},
                {"name": "__csrf", "value": "t"},
            ]
        }

    login._send = _fake_send  # type: ignore[method-assign]
    cookies = login.read_cookies()
    assert cookies == {"MUSIC_U": "u1", "__csrf": "t"}
    assert sent[0][0] == "Network.getCookies"
    assert sent[0][1] == {"urls": [bl.COOKIE_DOMAIN_URL]}


def test_open_login_page_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    login = BrowserLogin(profile_dir="p")
    calls: list[str] = []
    login._connect = calls.append  # type: ignore[method-assign]
    login.open_login_page()
    assert calls == [bl.LOGIN_URL]


# --------------------------------------------------------------------------- #
# wait_for_login
# --------------------------------------------------------------------------- #


def test_wait_for_login_returns_once_music_u_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = BrowserLogin(profile_dir="p")
    states: list[dict[str, str]] = [
        {},
        {"MUSIC_U": "u1", "other": "v"},
    ]

    def _fake_read() -> dict[str, str]:
        return states.pop(0) if states else {}

    login.read_cookies = _fake_read  # type: ignore[method-assign]
    monkeypatch.setattr(bl, "time", _fake_time(step=1.0))

    statuses: list[str] = []
    result = login.wait_for_login(
        timeout=60, on_status=lambda text, key: statuses.append(text)
    )
    assert result == {"MUSIC_U": "u1", "other": "v"}
    assert statuses == []


def test_wait_for_login_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    login = BrowserLogin(profile_dir="p")
    login.read_cookies = lambda: {}  # type: ignore[method-assign]
    clock = _fake_time()
    monkeypatch.setattr(bl, "time", clock)
    with pytest.raises(BrowserLoginError, match="已取消"):
        login.wait_for_login(timeout=60, should_stop=lambda: True)


def test_wait_for_login_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    login = BrowserLogin(profile_dir="p")
    login.read_cookies = lambda: {}  # type: ignore[method-assign]
    clock = _fake_time(step=10.0)
    monkeypatch.setattr(bl, "time", clock)
    statuses: list[str] = []
    with pytest.raises(BrowserLoginError, match="等待登录超时"):
        login.wait_for_login(
            timeout=30, on_status=lambda text, key: statuses.append(text)
        )
    assert any("等待登录中" in s for s in statuses)


def test_wait_for_login_keeps_polling_after_read_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = BrowserLogin(profile_dir="p")
    attempts = {"n": 0}

    def _flaky() -> dict[str, str]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("ws closed")
        return {"MUSIC_U": "late"}

    login.read_cookies = _flaky  # type: ignore[method-assign]
    clock = _fake_time()
    monkeypatch.setattr(bl, "time", clock)
    assert login.wait_for_login(timeout=60) == {"MUSIC_U": "late"}
    assert attempts["n"] == 2


# --------------------------------------------------------------------------- #
# close()
# --------------------------------------------------------------------------- #


def test_close_is_noop_when_browser_was_not_launched() -> None:
    login = BrowserLogin(profile_dir="p")
    login.close()  # must not raise and must not try to kill anything


def test_close_uses_cdp_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(version={"Browser": "X", "webSocketDebuggerUrl": "ws://b"})
    monkeypatch.setattr(bl, "requests", http)
    ws = FakeWs()
    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.websocket.create_connection",
        lambda *a, **k: ws,
    )

    killed: list[list[str]] = []

    def _no_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        killed.append(list(args[0]) if args else [])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.subprocess.run", _no_run
    )

    login = BrowserLogin(profile_dir="p")
    login.launched = True
    login.close()

    assert "Browser.close" in ws.sent[0]
    assert killed == []  # profile kill never needed


def test_close_falls_back_to_profile_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    profile = str(tmp_path / "prof")
    http = FakeHttp(version=None)
    monkeypatch.setattr(bl, "requests", http)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "powershell":
            out = (
                f"111|chrome.exe --user-data-dir={profile.upper()} --x\n"
                "222|chrome.exe --user-data-dir=C:\\other --x\n"
                "noseparator\n"
                f"333|msedge.exe --user-data-dir={profile}\n"
            )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=out)
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.subprocess.run", _fake_run
    )

    login = BrowserLogin(profile_dir=profile)
    login.launched = True
    login.close()

    taskkills = [c for c in calls if c[0] == "taskkill"]
    assert sorted(c[3] for c in taskkills) == ["111", "333"]


def test_close_ignores_powershell_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp(version=None)
    monkeypatch.setattr(bl, "requests", http)

    def _boom(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("powershell missing")

    monkeypatch.setattr(
        "novel_downloader.apps.music.browser_login.subprocess.run", _boom
    )
    login = BrowserLogin(profile_dir="p")
    login.launched = True
    login.close()  # must swallow the error
