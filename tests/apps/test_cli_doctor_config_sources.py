#!/usr/bin/env python3
"""
tests.apps.test_cli_doctor_config_sources
-----------------------------------------

Output-contract and failure-path tests for the diagnostic/configuration
commands: ``doctor``, ``config`` and ``sources``.

Every test is offline and deterministic. Network probes, plugin imports and
process state are replaced with small fakes; interface language is pinned to
English by ``tests/conftest.py``.
"""

from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import config, doctor, sources
from novel_downloader.apps.cli.commands.config import ConfigCmd
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd
from novel_downloader.apps.cli.commands.sources import SourcesCmd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parser(*commands: type) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _doctor_args(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "port": _free_port(),
        "as_json": False,
        "online": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# doctor: one broken probe must not abort the remaining checks
# ---------------------------------------------------------------------------


def test_doctor_collect_checks_isolates_a_crashing_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> Check:
        raise RuntimeError("check exploded")

    monkeypatch.setattr(doctor, "_check_python", _raise)

    checks = doctor.collect_checks(port=_free_port())
    by_key = {check.key: check for check in checks}

    crashed = by_key["python"]
    assert crashed.ok is False
    assert "check exploded" in crashed.detail
    # The crash must not stop collection: checks defined after ``python`` ran.
    assert {"package", "dependencies", "site_plugins", "music", "api_port"} <= set(
        by_key
    )
    assert len(checks) == 14


def test_doctor_treats_a_non_check_return_value_as_a_failure() -> None:
    row = doctor._run_check("python", "Python version", lambda: None)  # type: ignore[arg-type,return-value]

    assert row.ok is False
    assert "expected Check" in row.detail
    assert row.elapsed is not None


def test_doctor_json_is_single_line_and_always_has_stable_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = [
        Check("Python version", True, "3.11", key="python", elapsed=0.01),
        Check("Configuration", False, "boom", key="config", elapsed=0.02),
        Check("API port 1", True, "free", key="api_port", elapsed=0.0),
    ]
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: checks)

    with pytest.raises(SystemExit) as excinfo:
        DoctorCmd.run(_doctor_args(as_json=True))

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    keys = [check["key"] for check in payload["checks"]]
    assert keys == ["python", "config", "api_port"]
    for check in payload["checks"]:
        assert {"key", "name", "ok", "fatal", "elapsed"} <= set(check)
        assert isinstance(check["elapsed"], (int, float))

    assert payload["summary"]["failures"] == 1
    assert payload["summary"]["ok"] is False
    assert payload["warnings"] == 0
    assert payload["status"] == "failed"


def test_doctor_json_is_a_single_line_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda *_args, **_kwargs: [Check("Python version", True, "3.11", key="python")],
    )

    DoctorCmd.run(_doctor_args(as_json=True))

    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert len(out.strip().splitlines()) == 1
    assert json.loads(out)["checks"][0]["key"] == "python"


def test_doctor_optional_failure_is_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda *_args, **_kwargs: [
            Check("Python version", True, "3.11", key="python"),
            Check("Disk space", False, "low", fatal=False, key="disk_space"),
        ],
    )

    DoctorCmd.run(_doctor_args(as_json=True))  # must not raise SystemExit

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["failures"] == 0
    assert payload["summary"]["ok"] is True
    assert payload["warnings"] == 1
    assert payload["status"] == "warnings"


def test_doctor_human_output_summarizes_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = [
        Check("Python version", True, "3.11", key="python"),
        Check("Disk space", False, "low", fatal=False, key="disk_space"),
    ]
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: checks)
    messages = _capture_ui(monkeypatch, doctor)

    DoctorCmd.run(_doctor_args())

    assert any("warning" in line.lower() for line in messages.warn)
    assert not any("All checks passed" in line for line in messages.success)


def test_doctor_redacts_credentials_tokens_and_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "private" / "settings.toml"
    leaked = (
        f"cannot read {secret_path} via http://alice:s3cr3t@proxy.local:8080 "
        f"token=topsecret"
    )

    def _raise() -> Check:
        raise RuntimeError(leaked)

    monkeypatch.setattr(doctor, "_check_config", _raise)

    with pytest.raises(SystemExit):
        DoctorCmd.run(_doctor_args(as_json=True))

    out = capsys.readouterr().out
    assert "s3cr3t" not in out
    assert "topsecret" not in out
    assert str(tmp_path) not in out
    assert str(secret_path) not in out
    # The check itself is still reported, with the failure visible.
    payload = json.loads(out)
    config_row = next(row for row in payload["checks"] if row["key"] == "config")
    assert config_row["ok"] is False
    assert "cannot read" in config_row["detail"]


def test_doctor_absolute_configured_paths_are_masked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "downloads"
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda *_args, **_kwargs: [
            Check("Output directory", True, str(output_dir), key="output_dir"),
        ],
    )

    DoctorCmd.run(_doctor_args(as_json=True))

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["checks"][0]["key"] == "output_dir"
    detail = payload["checks"][0]["detail"]
    assert str(tmp_path) not in detail
    assert "downloads" in detail


# ---------------------------------------------------------------------------
# config: failures are reported, never a traceback
# ---------------------------------------------------------------------------


def test_config_set_lang_write_failure_exits_one_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_lang: str) -> str:
        raise OSError("read-only state")

    monkeypatch.setattr(config, "set_language", _boom)
    messages = _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "set-lang", "en"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    joined = " ".join(messages.error)
    assert "read-only state" in joined
    assert not any("Traceback" in line for line in messages.error)


def test_config_set_lang_unsupported_language_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "set_language", lambda _lang: "")
    messages = _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "set-lang", "xx"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported language" in line for line in messages.error)


# ---------------------------------------------------------------------------
# sources: stable JSON, empty registry, probe/registry failures
# ---------------------------------------------------------------------------


def _fake_rule(
    name: str = "Demo Source",
    *,
    base_url: str | None = "https://demo.example",
) -> Any:
    return SimpleNamespace(
        display_name=name,
        search=object(),
        hosts=("demo.example",),
        base_url=base_url,
    )


def test_sources_json_is_sorted_by_site_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [
            ("zeta", _fake_rule("Zeta"), {"origin": "user"}),
            ("alpha", _fake_rule("Alpha"), {"origin": "builtin"}),
        ],
    )

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--json"])
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert [row["site_key"] for row in payload] == ["alpha", "zeta"]
    assert [row["name"] for row in payload] == ["Alpha", "Zeta"]


def test_sources_empty_registry_json_is_an_empty_array(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("novel_downloader.plugins.rule.iter_rules", lambda: [])

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--json"])
    args.func(args)  # no SystemExit

    assert json.loads(capsys.readouterr().out) == []


def test_sources_check_json_has_machine_readable_status_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [("broken", _fake_rule(), {"origin": "user"})],
    )
    monkeypatch.setattr(
        SourcesCmd,
        "_probe",
        classmethod(lambda _cls, _key, _has_search: "FAIL: boom"),
    )

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--check", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "FAIL: boom"  # kept for compatibility
    assert row["status_code"] == "fail"
    assert row["search"] is True


def test_sources_probe_crash_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [("demo", _fake_rule(), {"origin": "builtin"})],
    )
    monkeypatch.setattr(
        sources,
        "_run_probes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe boom")),
    )
    messages = _capture_ui(monkeypatch, sources)

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--probe", "--json"])
    args.func(args)  # probe problems are advisory: exit 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["latency_ms"] is None
    assert payload[0]["http_status"] is None
    assert any("probe boom" in line for line in messages.warn)


def test_sources_registry_failure_exits_one_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> list[Any]:
        raise OSError("rules directory unreadable")

    monkeypatch.setattr("novel_downloader.plugins.rule.iter_rules", _boom)
    messages = _capture_ui(monkeypatch, sources)

    args = _parser(SourcesCmd).parse_args(["sources", "list"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("rules directory unreadable" in line for line in messages.error)
