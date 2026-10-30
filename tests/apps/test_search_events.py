#!/usr/bin/env python3
"""
tests.apps.test_search_events
-----------------------------

Offline tests for the per-site search progress lines of ``SearchCmd``.

The aggregate engine call (``plugins.search.search``) is replaced with a fake
that replays a scripted ``SearchEvent`` sequence through the ``on_event``
callback the command is expected to wire in.  No network, no registrar, no
prompting: everything is deterministic and host-independent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands.search import SearchCmd
from novel_downloader.plugins.search import SearchEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """Build a minimal top-level parser hosting only the search command."""
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    SearchCmd.register(subparsers)
    return parser


class _Messages:
    """Records the messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []


def _capture_ui(monkeypatch: pytest.MonkeyPatch) -> _Messages:
    from novel_downloader.apps.cli.commands import search as search_cmd

    messages = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(search_cmd.ui, name, getattr(messages, name).append)
    return messages


def _general_config(tmp_path: Path) -> dict[str, Any]:
    return {"general": {"debug": {"log_dir": str(tmp_path / "logs")}}}


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novel_downloader.apps.cli.commands import search as search_cmd

    monkeypatch.setattr(
        search_cmd, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )


def _skip_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the interactive result selection a no-op (user cancels)."""
    from novel_downloader.apps.cli.commands import search as search_cmd

    monkeypatch.setattr(
        search_cmd.prompts, "select_search_result", lambda *_a, **_k: None
    )


def _result(site: str, book_id: str) -> dict[str, Any]:
    """A minimal JSON-serializable ``SearchResult``-shaped dict."""
    return {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://{site}.example/book/{book_id}",
        "cover_url": "",
        "title": "Some Title",
        "author": "Some Author",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 0,
    }


def _install_fake_search(
    monkeypatch: pytest.MonkeyPatch,
    events: list[SearchEvent],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Replace ``plugins.search.search`` with a fake that replays ``events``
    through the ``on_event`` callback and returns ``results``.

    Returns the recorded call kwargs so tests can assert the wiring (e.g. that
    ``on_event`` was actually passed).
    """
    calls: list[dict[str, Any]] = []

    async def _fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        on_event = kwargs.get("on_event")
        for event in events:
            if on_event is not None:
                on_event(event)
        return list(results)

    monkeypatch.setattr("novel_downloader.plugins.search.search", _fake_search)
    return calls


# ---------------------------------------------------------------------------
# Interactive mode: per-site lines + summary on stderr
# ---------------------------------------------------------------------------


def test_interactive_search_prints_per_site_lines_and_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events = [
        SearchEvent("fanqienovel", "browser_skipped"),
        SearchEvent("b520", "completed", result_count=3),
        SearchEvent("dxswz", "completed", result_count=0),
        SearchEvent("czbooks", "failed", "HTTP 403"),
    ]
    _patch_config(monkeypatch, tmp_path)
    _skip_prompt(monkeypatch)
    messages = _capture_ui(monkeypatch)
    calls = _install_fake_search(monkeypatch, events, [_result("b520", "8_8187")])

    args = _parser().parse_args(["search", "some keyword"])
    args.func(args)

    # The callback must actually be wired into the engine call.
    assert calls and callable(calls[0].get("on_event"))

    assert "✓ b520 · 3 result(s)" in messages.success
    assert "✓ dxswz · 0 result(s)" in messages.success
    assert "✗ czbooks · Search failed: HTTP 403" in messages.warn
    assert "↷ fanqienovel · skipped (browser-assisted)" in messages.info
    assert "2 site(s) completed / 1 failed / 1 skipped" in messages.info


def test_interactive_search_marks_explicit_browser_assisted_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events = [
        SearchEvent("fanqienovel", "browser_assisted"),
        SearchEvent("fanqienovel", "completed", result_count=2),
    ]
    _patch_config(monkeypatch, tmp_path)
    _skip_prompt(monkeypatch)
    messages = _capture_ui(monkeypatch)
    _install_fake_search(monkeypatch, events, [_result("fanqienovel", "1")])

    args = _parser().parse_args(["search", "kw", "--site", "fanqienovel"])
    args.func(args)

    assert "↷ fanqienovel · browser-assisted" in messages.info
    assert "✓ fanqienovel · 2 result(s)" in messages.success
    assert "1 site(s) completed / 0 failed / 0 skipped" in messages.info
    assert messages.warn == []
    assert messages.error == []


def test_interactive_search_without_events_still_prints_zero_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Simulates a cache hit / empty site set: no events ever arrive.
    _patch_config(monkeypatch, tmp_path)
    messages = _capture_ui(monkeypatch)
    calls = _install_fake_search(monkeypatch, [], [])

    args = _parser().parse_args(["search", "kw"])
    args.func(args)

    assert calls and calls[0].get("on_event") is not None
    assert "0 site(s) completed / 0 failed / 0 skipped" in messages.info


# ---------------------------------------------------------------------------
# --json mode: stdout stays pure, summary goes to stderr
# ---------------------------------------------------------------------------


def test_json_mode_keeps_stdout_pure_and_summarizes_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = [
        SearchEvent("fanqienovel", "browser_skipped"),
        SearchEvent("b520", "completed", result_count=3),
        SearchEvent("czbooks", "failed", "HTTP 403"),
    ]
    results = [_result("b520", "8_8187"), _result("b520", "8_9999")]
    _patch_config(monkeypatch, tmp_path)
    messages = _capture_ui(monkeypatch)
    _install_fake_search(monkeypatch, events, results)

    args = _parser().parse_args(["search", "kw", "--json"])
    args.func(args)

    captured = capsys.readouterr()
    # stdout is exactly the JSON payload: no event/status lines leak into it.
    assert json.loads(captured.out) == results
    for token in ("✓", "✗", "↷", "site(s) completed", "result(s)"):
        assert token not in captured.out

    # No per-site lines were printed in JSON mode.
    assert messages.success == []
    assert messages.warn == []
    # The event summary is still reported, on stderr (ui.info).
    assert messages.info == ["1 site(s) completed / 1 failed / 1 skipped"]


def test_json_mode_without_events_prints_only_zero_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_config(monkeypatch, tmp_path)
    messages = _capture_ui(monkeypatch)
    _install_fake_search(monkeypatch, [], [])

    args = _parser().parse_args(["search", "kw", "--json"])
    args.func(args)

    assert json.loads(capsys.readouterr().out) == []
    assert messages.info == ["0 site(s) completed / 0 failed / 0 skipped"]
