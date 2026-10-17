#!/usr/bin/env python3
"""Tests for the browser-assisted search helper."""

import json
import subprocess
import sys
from typing import Any

import pytest

from novel_downloader.apps.browser import (
    BrowserSearchSpec,
    browser_search_main,
    capture_search_json,
    resolve_spec,
    run_browser_search,
    search_via_browser,
)
from novel_downloader.apps.browser import main as browser_main


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


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python"], returncode=returncode, stdout=stdout
    )


def test_search_via_browser_reads_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    noise = "some pywebview chatter\n"
    payload = json.dumps({"ok": True, "body": '{"code": 0}'})
    monkeypatch.setattr(
        browser_main.subprocess,
        "run",
        lambda *a, **k: _completed(f"{noise}{payload}\n"),
    )
    assert search_via_browser("fanqienovel", "斗破苍穹") == '{"code": 0}'


def test_search_via_browser_ignores_unusable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_main.subprocess, "run", lambda *a, **k: _completed("not json\n")
    )
    assert search_via_browser("fanqienovel", "kw") is None

    monkeypatch.setattr(
        browser_main.subprocess,
        "run",
        lambda *a, **k: _completed(json.dumps({"ok": False, "body": ""})),
    )
    assert search_via_browser("fanqienovel", "kw") is None


def test_search_via_browser_handles_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise OSError("no python")

    monkeypatch.setattr(browser_main.subprocess, "run", _boom)
    assert search_via_browser("fanqienovel", "kw") is None


def test_browser_search_main_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        browser_main, "run_browser_search", lambda *a, **k: '{"code": 0}'
    )
    assert browser_search_main(["fanqienovel", "斗破苍穹"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"ok": True, "body": '{"code": 0}'}

    monkeypatch.setattr(browser_main, "run_browser_search", lambda *a, **k: None)
    assert browser_search_main(["fanqienovel", "斗破苍穹"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "body": ""}


def test_run_browser_search_rejects_unsupported_site() -> None:
    messages: list[str] = []
    assert run_browser_search("no-such-site", "kw", notify=messages.append) is None
    assert any("does not support" in message for message in messages)


def test_run_browser_search_reports_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_main, "capture_search_json", lambda *a, **k: None)
    messages: list[str] = []
    assert run_browser_search("fanqienovel", "kw", notify=messages.append) is None
    assert any("returned no data" in message for message in messages)


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
