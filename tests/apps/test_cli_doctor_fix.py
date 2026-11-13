#!/usr/bin/env python3
"""
tests.apps.test_cli_doctor_fix
------------------------------

Focused tests for the opt-in ``novel-cli doctor --fix`` pass.

Everything runs against ``tmp_path``: no configuration is written, no network
is touched and every directory the fixer may act on is redirected explicitly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import doctor
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd

_DAY = 86400.0


def _doctor_args(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "port": 8081,
        "as_json": True,
        "online": False,
        "fix": False,
        "dry_run": False,
        "fix_age_days": doctor.DEFAULT_FIX_MAX_AGE_DAYS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _passing_checks(*_args: Any, **_kwargs: Any) -> list[Check]:
    return [Check("Python version", True, "3.11", key="python")]


def _point_dirs(
    monkeypatch: pytest.MonkeyPatch,
    output: Path,
    cache: Path,
    log: Path,
) -> None:
    monkeypatch.setattr(doctor, "_output_dir", lambda: output)
    monkeypatch.setattr(doctor, "_cache_dir", lambda: cache)
    monkeypatch.setattr(doctor, "_log_dir", lambda: log)


def _write_aged(path: Path, *, age_days: float, size: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = 1_700_000_000.0
    os.utime(path, (timestamp - age_days * _DAY, timestamp - age_days * _DAY))
    return path


def test_fix_creates_missing_output_cache_and_log_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "downloads"
    cache = tmp_path / "cache"
    log = tmp_path / "logs"
    _point_dirs(monkeypatch, output, cache, log)

    plan = doctor.plan_fixes(max_age_days=7, now=1_700_000_000.0)
    assert {item.action for item in plan.actions} == {doctor.FIX_CREATE_DIR}

    report = doctor.apply_fixes(plan, max_age_days=7)

    assert report.errors == []
    assert output.is_dir()
    assert cache.is_dir()
    assert log.is_dir()
    assert len(report.fixed) == 3
    assert {row["action"] for row in report.fixed} == {doctor.FIX_CREATE_DIR}


def test_fix_removes_only_stale_tmp_and_part_files(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    old_tmp = _write_aged(root / "old.tmp", age_days=8)
    old_part = _write_aged(root / "nested" / "old.part", age_days=30)
    fresh_tmp = _write_aged(root / "fresh.tmp", age_days=1)
    keep = _write_aged(root / "keep.txt", age_days=30)

    now = 1_700_000_000.0
    plan = doctor.plan_fixes(roots=[root], max_age_days=7, now=now)
    report = doctor.apply_fixes(plan, max_age_days=7)

    assert report.errors == []
    assert not old_tmp.exists()
    assert not old_part.exists()
    assert fresh_tmp.exists()
    assert keep.exists()
    removed = [row for row in report.fixed if row["action"] == doctor.FIX_REMOVE_FILE]
    assert len(removed) == 2
    assert sum(int(row["bytes"]) for row in removed) == 6


def test_fix_refuses_a_candidate_outside_the_allowed_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    outside = _write_aged(tmp_path / "outside.tmp", age_days=30)

    # Simulate a walker handing back a path that escaped the root (a symlink
    # component or a renamed file between plan and apply would do the same).
    monkeypatch.setattr(doctor, "_iter_temp_files", lambda _root: [outside])

    plan = doctor.plan_fixes(roots=[root], max_age_days=7, now=1_700_000_000.0)
    assert plan.actions == []
    assert plan.skipped
    assert plan.skipped[0]["reason"] == "outside the allowed root"

    report = doctor.apply_fixes(plan, max_age_days=7)
    assert outside.exists()
    assert report.fixed == []


def test_fix_skips_a_symlinked_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    target = _write_aged(tmp_path / "target.tmp", age_days=30)
    root.mkdir()
    link = root / "link.tmp"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows policy
        pytest.skip("symlink creation is not permitted on this host")

    plan = doctor.plan_fixes(roots=[root], max_age_days=7, now=1_700_000_000.0)

    assert plan.actions == []
    assert plan.skipped[0]["reason"] == "path traverses a symlink"
    assert target.exists()


def test_dry_run_makes_no_filesystem_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "downloads"
    cache = tmp_path / "cache"
    log = tmp_path / "logs"
    cache.mkdir()
    stale = _write_aged(cache / "stale.part", age_days=30)
    _point_dirs(monkeypatch, output, cache, log)
    monkeypatch.setattr(doctor, "collect_checks", _passing_checks)

    DoctorCmd.run(_doctor_args(fix=True, dry_run=True))  # no SystemExit

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["fixed"] == []
    assert any(row["reason"] == "dry_run" for row in payload["skipped"])
    assert not output.exists()
    assert not log.exists()
    assert stale.exists()


def test_dry_run_directory_probe_does_not_create_anything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "downloads"
    log = tmp_path / "logs"
    monkeypatch.setattr(doctor, "_output_dir", lambda: output)
    monkeypatch.setattr(doctor, "_log_dir", lambda: log)

    output_check = doctor._check_output_dir(create=False)
    log_check = doctor._check_log_dir(create=False)

    assert output_check.ok is True
    assert "will create" in output_check.detail
    assert log_check.ok is True
    assert not output.exists()
    assert not log.exists()


def test_fix_permission_failure_is_reported_and_the_rest_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    created = tmp_path / "created"

    def _fail_blocked(path: Path) -> None:
        if path == blocked:
            raise PermissionError("permission denied")
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(doctor, "_create_dir", _fail_blocked)

    plan = doctor.plan_fixes(roots=[blocked, created], now=1_700_000_000.0)
    report = doctor.apply_fixes(plan, max_age_days=7)

    assert not blocked.exists()
    assert created.is_dir()
    assert len(report.errors) == 1
    assert len(report.fixed) == 1
    assert "permission denied" in report.errors[0]["error"]
    assert report.ok is False


def test_fix_removal_failure_is_aggregated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    first = _write_aged(root / "first.tmp", age_days=30)
    second = _write_aged(root / "second.tmp", age_days=30)

    def _fail_first(path: Path) -> None:
        if path == first:
            raise PermissionError("file is busy")
        path.unlink()

    monkeypatch.setattr(doctor, "_remove_file", _fail_first)

    plan = doctor.plan_fixes(roots=[root], max_age_days=7, now=1_700_000_000.0)
    report = doctor.apply_fixes(plan, max_age_days=7)

    assert first.exists()
    assert not second.exists()
    assert len(report.errors) == 1
    assert "busy" in report.errors[0]["error"]


def test_fix_json_exposes_stable_fields_and_a_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = tmp_path / "blocked"

    def _always_fails(_path: Path) -> None:
        raise PermissionError("read-only parent")

    monkeypatch.setattr(doctor, "_output_dir", lambda: blocked)
    monkeypatch.setattr(doctor, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(doctor, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(doctor, "_create_dir", _always_fails)
    monkeypatch.setattr(doctor, "collect_checks", _passing_checks)

    with pytest.raises(SystemExit) as excinfo:
        DoctorCmd.run(_doctor_args(fix=True, as_json=True))

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    payload = json.loads(out)
    assert {"fixed", "skipped", "errors"} <= set(payload)
    assert payload["errors"]
    assert payload["summary"]["ok"] is False
    assert payload["status"] == "failed"


def test_fix_age_threshold_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _point_dirs(monkeypatch, tmp_path / "out", tmp_path / "cache", tmp_path / "logs")

    with pytest.raises(SystemExit) as excinfo:
        DoctorCmd.run(_doctor_args(fix=True, fix_age_days=0))

    assert excinfo.value.code == 2
    assert not (tmp_path / "out").exists()


def test_validate_fix_path_rejects_parent_references_and_outside_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert doctor._validate_fix_path(root / "ok.tmp", root) == ""
    assert "outside" in doctor._validate_fix_path(tmp_path / "nope.tmp", root)
    assert "parent reference" in doctor._validate_fix_path(
        root / ".." / "escape.tmp", root
    )
