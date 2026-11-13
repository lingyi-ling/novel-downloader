#!/usr/bin/env python3
"""
tests.apps.test_cli_search
--------------------------

Offline contract tests for ``novel-cli search``.

The aggregate search is stubbed, so every test here is deterministic and
network-free.  The focus is the machine-readable contract (``--json``), the
numeric options (``--limit`` / ``--site-limit``) and multi-word queries.
"""

from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from datetime import date
from typing import Any

import pytest

from novel_downloader.apps.cli.commands.base import EXIT_USAGE
from novel_downloader.apps.cli.commands.search import SearchCmd
from novel_downloader.plugins.search import SearchEvent

#: Every field the documented flat ``--json`` row carries.  ``sources`` is
#: added by ``--group`` on top of these.
BASE_FIELDS = {
    "site",
    "book_id",
    "book_url",
    "cover_url",
    "title",
    "author",
    "latest_chapter",
    "update_date",
    "word_count",
    "priority",
    "rank_score",
    "match_tier",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    SearchCmd.register(subparsers)
    return parser


def _args(argv: list[str]) -> argparse.Namespace:
    return _parser().parse_args(["search", *argv])


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "site": "beta",
        "book_id": "beta-1",
        "book_url": "https://example.invalid/beta/1",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "第一章",
        "update_date": "2026-01-01",
        "word_count": "30万字",
        "priority": 100,
        "rank_score": 0.875,
        "match_tier": 4,
    }
    row.update(overrides)
    return row


@pytest.fixture
def search_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub config/logging/status so the command never touches disk or tty."""
    from novel_downloader.apps.cli.commands import search as module

    monkeypatch.setattr(
        module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(module.ui, "setup_logging", lambda **_k: None)
    monkeypatch.setattr(module.ui, "status", lambda _message: nullcontext())
    # Selection must never block a test; individual tests override it.
    monkeypatch.setattr(module.prompts, "select_search_result", lambda _r: None)
    return module


def _install_search(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, Any]],
    *,
    events: tuple[SearchEvent, ...] = (),
    captured: dict[str, Any] | None = None,
) -> None:
    async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        if captured is not None:
            captured.update(kwargs)
        for event in events:
            kwargs["on_event"](event)
        return list(results)

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)


def _run(
    args: argparse.Namespace,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    code = 0
    try:
        args.func(args)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    out, err = capsys.readouterr()
    return code, out, err


def test_local_plugin_searchers_are_enabled_before_search(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``search`` must honour ``[plugins] enable_local_plugins``.

    Without the registry hook a site provided by a local plugin is simply not
    registered: ``search --site <plugin> --json`` printed ``[]`` and exited 0
    with "0 site(s) completed", which looks like "no results" rather than the
    configuration being ignored.  ``download``/``export``/``batch`` already
    performed this step.
    """
    monkeypatch.setattr(
        search_module,
        "load_or_init_config",
        lambda *_a, **_k: {
            "general": {},
            "plugins": {
                "enable_local_plugins": True,
                "local_plugins_path": "/tmp/local-plugins",
                "override_builtins": True,
            },
        },
    )
    enabled: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        search_module.registrar,
        "enable_local_plugins",
        lambda path=None, override=False: enabled.append((path, override)),
    )
    _install_search(monkeypatch, [_row()])

    code, out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert enabled == [("/tmp/local-plugins", True)]
    assert json.loads(out)[0]["book_id"] == "beta-1"


# ---------------------------------------------------------------------------
# --json: single line, stable fields, pure stdout
# ---------------------------------------------------------------------------


def test_json_output_is_one_line_and_keeps_documented_fields(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [_row()],
        events=(SearchEvent(site="beta", kind="completed", result_count=1),),
    )

    code, out, err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    # A line-oriented consumer must not have to reassemble the document.
    assert len(out.splitlines()) == 1
    payload = json.loads(out)
    assert isinstance(payload, list) and len(payload) == 1
    assert set(payload[0]) >= BASE_FIELDS
    assert payload[0]["rank_score"] == 0.875
    assert payload[0]["match_tier"] == 4
    assert "sources" not in payload[0]  # grouping is opt-in
    # Diagnostics stay on stderr and carry no ANSI when the stream is a pipe.
    assert "1 site(s) completed / 0 failed / 0 skipped" in err
    assert "\x1b" not in out + err


def test_json_group_output_is_one_line_and_keeps_sources(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _row(sources=["beta", "alpha"])
    _install_search(monkeypatch, [row])

    code, out, _err = _run(_args(["--json", "--group", "三体"]), capsys)

    assert code == 0
    assert len(out.splitlines()) == 1
    payload = json.loads(out)
    assert payload[0]["sources"] == ["beta", "alpha"]
    assert set(payload[0]) >= BASE_FIELDS


def test_json_failure_details_stay_off_stdout(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(
            SearchEvent(site="b520", kind="failed", message="timeout"),
            SearchEvent(site="qidian", kind="completed", result_count=0),
        ),
    )

    code, out, err = _run(_args(["--json", "斗破苍穹"]), capsys)

    assert code == 0  # one site answered: not an outage
    assert json.loads(out) == []
    # Per-site lines are suppressed in JSON mode; only the tally is reported,
    # and it lives on stderr so stdout stays a parseable document.
    assert "1 site(s) completed / 1 failed / 0 skipped" in err
    for token in ("✗", "Search failed", "✓", "↷"):
        assert token not in out
    assert "\x1b" not in out + err


def test_json_empty_result_warns_with_keyword(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(SearchEvent(site="b520", kind="completed", result_count=0),),
    )

    code, out, err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert json.loads(out) == []
    assert "No results found for '三体'." in err


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_all_sites_failed_emits_empty_array_and_exits_one(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(
            SearchEvent(site="b520", kind="failed", message="timeout"),
            SearchEvent(site="qidian", kind="failed", message="refused"),
        ),
    )

    code, out, err = _run(_args(["--json", "斗破苍穹"]), capsys)

    assert code == 1
    assert json.loads(out) == []
    assert "0 site(s) completed / 2 failed / 0 skipped" in err
    # The outage is not reported as "no such book".
    assert "No results found" not in err


def test_json_and_first_is_a_usage_error(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[Any] = []

    async def fake_search(**kwargs: Any) -> list[Any]:  # pragma: no cover
        called.append(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code, out, err = _run(_args(["--json", "--first", "三体"]), capsys)

    assert code == 2
    assert out == ""
    assert "--json" in err and "--first" in err
    assert called == []


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("flag", "value"), [("--limit", "0"), ("--limit", "-5")])
def test_non_positive_overall_limit_clamps_to_one(
    flag: str,
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, _err = _run(_args(["--json", flag, value, "三体"]), capsys)

    assert code == 0
    assert captured["limit"] == 1
    assert json.loads(out)


@pytest.mark.parametrize(
    ("flag", "value"), [("--site-limit", "0"), ("--site-limit", "-3")]
)
def test_non_positive_site_limit_clamps_to_one(
    flag: str,
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", flag, value, "三体"]), capsys)

    assert code == 0
    assert captured["per_site_limit"] == 1


def test_huge_limits_are_passed_through_without_crashing(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    huge = 10**15
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, _err = _run(
        _args(["--json", "--limit", str(huge), "--site-limit", str(huge), "三体"]),
        capsys,
    )

    assert code == 0
    assert captured["limit"] == huge
    assert captured["per_site_limit"] == huge
    assert len(json.loads(out)) == 1


# ---------------------------------------------------------------------------
# Keyword handling
# ---------------------------------------------------------------------------


def test_multi_word_query_is_joined_and_forwarded(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    # Unquoted "title author": each shell token is a separate argv element.
    code, _out, _err = _run(_args(["--json", "斗破苍穹", "天蚕土豆"]), capsys)

    assert code == 0
    assert captured["keyword"] == "斗破苍穹 天蚕土豆"


def test_quoted_multi_word_query_survives_untouched(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "斗破苍穹 天蚕土豆"]), capsys)

    assert code == 0
    assert captured["keyword"] == "斗破苍穹 天蚕土豆"


def test_keyword_is_stripped_before_use_and_echo(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, err = _run(_args(["--json", "  三体  "]), capsys)

    assert code == 0
    assert captured["keyword"] == "三体"
    assert "No results found for '三体'." in err


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_keyword_is_a_usage_error(
    blank: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[Any] = []

    async def fake_search(**kwargs: Any) -> list[Any]:  # pragma: no cover
        calls.append(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    loaded: list[Any] = []
    monkeypatch.setattr(
        search_module,
        "load_or_init_config",
        lambda *a, **k: loaded.append((a, k)) or {"general": {}},
    )

    code, out, err = _run(_args(["--json", blank]), capsys)

    assert code == 2
    assert out == ""
    assert "No keyword entered." in err
    # A blank query is rejected before any site or config work happens.
    assert calls == []
    assert loaded == []


def test_multi_word_query_is_echoed_by_the_human_summary(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(monkeypatch, [])

    code, _out, err = _run(_args(["斗破苍穹", "天蚕土豆"]), capsys)

    assert code == 0
    assert "No results found for '斗破苍穹 天蚕土豆'." in err


# ---------------------------------------------------------------------------
# Site selection / export behaviour
# ---------------------------------------------------------------------------


def test_site_and_limit_and_first_reach_the_aggregate_search(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(
        _args(["--first", "--limit", "3", "--site", "b520", "三体"]), capsys
    )

    assert code == 0
    assert captured["sites"] == ["b520"]
    assert captured["limit"] == 3
    assert captured["per_site_limit"] == 10


def test_json_mode_never_downloads_or_exports(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(monkeypatch, [_row()])

    def _unexpected(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise AssertionError("--json must not start a download")

    monkeypatch.setattr(search_module.registrar, "get_client", _unexpected)

    # ``--format`` is accepted but irrelevant: JSON mode prints and exits.
    # (The keyword comes first because ``--format`` is greedy by design.)
    code, out, _err = _run(_args(["--json", "三体", "--format", "epub"]), capsys)

    assert code == 0
    assert json.loads(out)[0]["book_id"] == "beta-1"


def test_no_results_with_first_exits_zero_without_download(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(SearchEvent(site="b520", kind="completed", result_count=0),),
    )

    def _unexpected(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise AssertionError("empty result set must not start a download")

    monkeypatch.setattr(search_module.registrar, "get_client", _unexpected)

    code, out, err = _run(_args(["--first", "三体"]), capsys)

    assert code == 0
    assert out == ""
    assert "No results found for '三体'." in err


# --------------------------------------------------------------------------- #
# optional local filters
# --------------------------------------------------------------------------- #


def test_local_filters_reach_the_aggregate_search(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(
        _args(
            [
                "--json",
                "--min-words",
                "10000",
                "--max-words",
                "500000",
                "--updated-after",
                "2024-01-01",
                "三体",
            ]
        ),
        capsys,
    )

    assert code == 0
    assert captured["min_words"] == 10000
    assert captured["max_words"] == 500000
    assert captured["updated_after"] == date(2024, 1, 1)


def test_local_filters_default_to_no_filtering(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["min_words"] is None
    assert captured["max_words"] is None
    assert captured["updated_after"] is None


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--min-words", "-1"),
        ("--min-words", "1000000001"),
        ("--max-words", "-1"),
        ("--max-words", "1000000001"),
    ],
)
def test_word_filters_reject_out_of_range_values(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, err = _run(_args(["--json", flag, value, "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert flag in err
    assert "min_words" not in captured


@pytest.mark.parametrize("value", ["2024/01/01", "20240101", "2024-13-01", "soon"])
def test_updated_after_rejects_non_iso_dates(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, err = _run(_args(["--json", "--updated-after", value, "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "--updated-after" in err
    assert "updated_after" not in captured
