"""Tests for the site health history/diff tooling.

These tests are fully offline: every site check is replaced with an async
stub, so no request ever leaves the process.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "check_sites_health.py"


@pytest.fixture(scope="module")
def health() -> Any:
    """Load ``scripts/check_sites_health.py`` without making it a package."""
    spec = importlib.util.spec_from_file_location("check_sites_health", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(
    health: Any,
    site: str,
    status: str,
    *,
    elapsed: float = 0.1,
    reason: str = "",
    result_count: int = 0,
) -> Any:
    return health.SearchHealthResult(
        site=site,
        searcher="FakeSearcher",
        status=status,
        elapsed=elapsed,
        result_count=result_count,
        reason=reason,
    )


def _args(health: Any, history: Path, **overrides: Any) -> argparse.Namespace:
    """Minimal namespace accepted by ``run_search_mode``."""
    values: dict[str, Any] = {
        "mode": "search",
        "timeout": 2.0,
        "concurrency": 2,
        "keyword": "魔法",
        "limit": 1,
        "no_progress": True,
        "diff": False,
        "history": history,
        "json": False,
        "report": None,
        "fail_on_error": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_checker(
    health: Any,
    statuses: dict[str, str],
    *,
    elapsed: float = 0.05,
) -> Any:
    """Build a deterministic async replacement for ``check_search_site``."""

    async def fake_check(
        session: Any,
        site: str,
        keyword: str,
        limit: int,
        timeout: float,
    ) -> Any:
        status = statuses[site]
        reason = {
            health.STATUS_PASS: "",
            health.STATUS_EMPTY: "no_results",
            health.STATUS_ERROR: "timeout",
        }[status]
        return _result(
            health,
            site,
            status,
            elapsed=elapsed,
            reason=reason,
            result_count=1 if status == health.STATUS_PASS else 0,
        )

    return fake_check


def _install_checker(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    statuses: dict[str, str],
    *,
    elapsed: float = 0.05,
) -> None:
    """Replace the real network check with a deterministic stub."""
    monkeypatch.setattr(
        health,
        "check_search_site",
        _make_checker(health, statuses, elapsed=elapsed),
    )


# ----------------------------
# Record building / JSONL I/O
# ----------------------------


def test_build_history_record_shape(health: Any) -> None:
    record = health.build_history_record(
        [
            _result(
                health, "alpha", health.STATUS_PASS, elapsed=0.123456, result_count=2
            ),
            _result(
                health, "beta", health.STATUS_EMPTY, elapsed=0.5, reason="no_results"
            ),
            _result(
                health, "gamma", health.STATUS_ERROR, elapsed=1.2344, reason="timeout"
            ),
        ],
        keyword="魔法",
        generated_at=datetime(2026, 10, 24, 12, 0, 0),
    )

    assert record == {
        "ts": "2026-10-24T12:00:00",
        "keyword": "魔法",
        "summary": {"pass": 1, "empty": 1, "error": 1},
        "results": [
            {"site": "alpha", "status": "PASS", "elapsed": 0.123, "reason": ""},
            {
                "site": "beta",
                "status": "EMPTY",
                "elapsed": 0.5,
                "reason": "no_results",
            },
            {
                "site": "gamma",
                "status": "ERROR",
                "elapsed": 1.234,
                "reason": "timeout",
            },
        ],
    }


def test_append_history_roundtrip(health: Any, tmp_path: Path) -> None:
    path = tmp_path / "nested" / "history.jsonl"
    first = {"ts": "2026-10-23T00:00:00", "keyword": "剑", "summary": {}, "results": []}
    second = {
        "ts": "2026-10-24T00:00:00",
        "keyword": "魔法",
        "summary": {},
        "results": [],
    }

    health.append_history(path, first)
    health.append_history(path, second)

    assert health.load_history(path) == [first, second]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["keyword"] == "剑"
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_load_history_missing_file(health: Any, tmp_path: Path) -> None:
    assert health.load_history(tmp_path / "absent.jsonl") == []


def test_load_history_skips_malformed_lines(health: Any, tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    valid_one = {"ts": "2026-10-23T00:00:00", "keyword": "one", "results": []}
    valid_two = {"ts": "2026-10-24T00:00:00", "keyword": "two", "results": []}
    path.write_text(
        "\n".join(
            [
                json.dumps(valid_one),
                "",
                "   ",
                "not json at all",
                "[1, 2, 3]",
                '{"broken": tru}',
                json.dumps(valid_two),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert health.load_history(path) == [valid_one, valid_two]


# ----------------------------
# Diff computation
# ----------------------------


def test_compute_diff_reports_transitions(health: Any) -> None:
    previous = {
        "ts": "2026-10-23T00:00:00",
        "results": [
            {"site": "alpha", "status": "PASS", "reason": ""},
            {"site": "beta", "status": "ERROR", "reason": "http_500"},
            {"site": "gamma", "status": "EMPTY", "reason": "no_results"},
            {"site": "removed", "status": "PASS", "reason": ""},
            # Malformed entries must be ignored, not crash the diff.
            "junk",
            {"site": None, "status": "PASS"},
            {"site": "nonsense", "status": 42},
        ],
    }
    current = {
        "ts": "2026-10-24T00:00:00",
        "results": [
            {"site": "alpha", "status": "ERROR", "reason": "timeout"},
            {"site": "beta", "status": "PASS", "reason": ""},
            {"site": "gamma", "status": "EMPTY", "reason": "no_results"},
            {"site": "added", "status": "PASS", "reason": ""},
        ],
    }

    assert health.compute_diff(previous, current) == [
        {"site": "added", "before": "-", "after": "PASS", "reason": ""},
        {"site": "alpha", "before": "PASS", "after": "ERROR", "reason": "timeout"},
        {"site": "beta", "before": "ERROR", "after": "PASS", "reason": "http_500"},
        {"site": "removed", "before": "PASS", "after": "-", "reason": ""},
    ]


def test_compute_diff_identical_records(health: Any) -> None:
    record = {
        "results": [
            {"site": "alpha", "status": "PASS", "reason": ""},
            {"site": "beta", "status": "ERROR", "reason": "timeout"},
        ]
    }
    assert health.compute_diff(record, record) == []


# ----------------------------
# Search-mode run + exit codes
# ----------------------------


def test_run_appends_history_and_prints_diff(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "history.jsonl"

    _install_checker(
        health,
        monkeypatch,
        {"alpha": health.STATUS_PASS, "beta": health.STATUS_ERROR},
    )
    assert (
        asyncio.run(health.run_search_mode(_args(health, path), ["alpha", "beta"])) == 0
    )
    capsys.readouterr()

    records = health.load_history(path)
    assert len(records) == 1
    assert records[0]["summary"] == {"pass": 1, "empty": 0, "error": 1}
    assert [item["site"] for item in records[0]["results"]] == ["alpha", "beta"]

    _install_checker(
        health,
        monkeypatch,
        {"alpha": health.STATUS_ERROR, "beta": health.STATUS_PASS},
    )
    args = _args(health, path, diff=True)
    assert asyncio.run(health.run_search_mode(args, ["alpha", "beta"])) == 0
    output = capsys.readouterr().out

    records = health.load_history(path)
    assert len(records) == 2
    assert records[-1]["summary"] == {"pass": 1, "empty": 0, "error": 1}
    assert health.compute_diff(records[-2], records[-1]) == [
        {"site": "alpha", "before": "PASS", "after": "ERROR", "reason": "timeout"},
        {"site": "beta", "before": "ERROR", "after": "PASS", "reason": "timeout"},
    ]
    assert "History Diff" in output
    assert "no status changes" not in output

    # A third run with the same statuses reports no changes.
    assert asyncio.run(health.run_search_mode(args, ["alpha", "beta"])) == 0
    output = capsys.readouterr().out
    assert "no status changes" in output
    assert len(health.load_history(path)) == 3


def test_run_diff_without_previous_history(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "history.jsonl"
    _install_checker(health, monkeypatch, {"alpha": health.STATUS_PASS})

    args = _args(health, path, diff=True)
    assert asyncio.run(health.run_search_mode(args, ["alpha"])) == 0

    output = capsys.readouterr().out
    assert "no previous record" in output
    assert len(health.load_history(path)) == 1


def test_json_payload_includes_diff(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "history.jsonl"
    health.append_history(
        path,
        {
            "ts": "2026-10-23T00:00:00",
            "keyword": "魔法",
            "summary": {"pass": 1, "empty": 0, "error": 0},
            "results": [
                {"site": "alpha", "status": "PASS", "elapsed": 0.1, "reason": ""}
            ],
        },
    )
    _install_checker(health, monkeypatch, {"alpha": health.STATUS_ERROR})

    args = _args(health, path, diff=True, json=True)
    exit_code = asyncio.run(health.run_search_mode(args, ["alpha"]))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["keyword"] == "魔法"
    assert payload["history"] == str(path)
    assert payload["summary"] == {
        "total": 1,
        "pass": 0,
        "empty": 0,
        "error": 1,
        "reasons": {"timeout": 1},
    }
    assert payload["diff"] == {
        "baseline": "2026-10-23T00:00:00",
        "changes": [
            {"site": "alpha", "before": "PASS", "after": "ERROR", "reason": "timeout"}
        ],
    }
    assert len(health.load_history(path)) == 2


def test_fail_on_error_exit_codes(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checker(health, monkeypatch, {"alpha": health.STATUS_ERROR})

    assert (
        asyncio.run(
            health.run_search_mode(
                _args(health, tmp_path / "a.jsonl", fail_on_error=True), ["alpha"]
            )
        )
        == 1
    )
    assert (
        asyncio.run(
            health.run_search_mode(_args(health, tmp_path / "b.jsonl"), ["alpha"])
        )
        == 0
    )

    _install_checker(health, monkeypatch, {"alpha": health.STATUS_PASS})
    assert (
        asyncio.run(
            health.run_search_mode(
                _args(health, tmp_path / "c.jsonl", fail_on_error=True), ["alpha"]
            )
        )
        == 0
    )


# ----------------------------
# Offline classification of a single check
# ----------------------------


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_reason"),
    [
        ("results", "PASS", ""),
        ("empty", "EMPTY", "no_results"),
        ("raise", "ERROR", "parse_error:ValueError"),
    ],
)
def test_check_search_site_classification(
    health: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    class FakeSearcher:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def search(self, keyword: str, limit: int = 1) -> list[dict[str, str]]:
            if outcome == "raise":
                raise ValueError("bad payload")
            if outcome == "empty":
                return []
            return [{"title": "x"}]

    monkeypatch.setattr(
        health.registrar, "get_searcher_class", lambda site: FakeSearcher
    )

    result = asyncio.run(
        health.check_search_site(
            session=object(),
            site="alpha",
            keyword="魔法",
            limit=1,
            timeout=2.0,
        )
    )

    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.site == "alpha"


# ----------------------------
# CLI wiring
# ----------------------------


def test_parser_history_defaults_and_flags(health: Any) -> None:
    parser = health.build_parser()

    defaults = parser.parse_args([])
    assert defaults.history == health.DEFAULT_HISTORY_PATH
    assert defaults.diff is False
    assert defaults.fail_on_error is False

    configured = parser.parse_args(
        ["--history", "custom.jsonl", "--diff", "--fail-on-error"]
    )
    assert configured.history == Path("custom.jsonl")
    assert configured.diff is True
    assert configured.fail_on_error is True
