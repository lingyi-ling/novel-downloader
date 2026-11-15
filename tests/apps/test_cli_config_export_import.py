#!/usr/bin/env python3
"""
tests.apps.test_cli_config_export_import
----------------------------------------

Focused tests for ``novel-cli config export`` and ``config import``.

The contract pinned here:

* export prepends a version/timestamp header, keeps every configuration byte
  (including a proxy password) untouched, and writes atomically;
* an existing destination is never replaced without ``--force``; ``--force``
  first keeps the previous bytes in ``<name>.bak``;
* import parses and schema-checks the source *before* writing, so a corrupt,
  wrong-schema or non-configuration TOML leaves the active file unchanged;
* a successful import backs up the previous active file and then replaces it
  atomically;
* directories are rejected on both sides and ``--json`` emits exactly one
  machine-readable line whose payload never contains configuration values.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import config
from novel_downloader.apps.cli.commands.config import ConfigCmd
from novel_downloader.infra.i18n import t

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Messages:
    """Records messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []

    def all_text(self) -> str:
        return "\n".join([*self.info, *self.success, *self.warn, *self.error])


def _capture_ui(monkeypatch: pytest.MonkeyPatch) -> _Messages:
    messages = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(config.ui, name, getattr(messages, name).append)
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ConfigCmd.register(subparsers)
    return parser


def _run(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run in an empty work directory with an empty application home."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NOVEL_HOME", str(home))
    # The tests assert English source strings; without the override the
    # default interface language (zh_CN) would translate them.
    monkeypatch.setenv("NOVEL_LANG", "en")
    return work


def _secret_config(workers: int = 3) -> str:
    return (
        "[general]\n"
        f"workers = {workers}\n"
        'proxy = "http://user:supersecret@127.0.0.1:7890"\n'
    )


def _json_out(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "JSON output must be a single line"
    assert out.endswith("\n")
    return json.loads(out)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_writes_header_and_preserves_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    settings = work / "settings.toml"
    settings.write_text(_secret_config(), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "export", "--out", "backup.toml", "--json"])

    payload = _json_out(capsys)
    assert payload["command"] == "config export"
    assert payload["status"] == "created"
    assert payload["path"] == str(work / "backup.toml")
    assert payload["source"] == str(settings)
    assert payload["backup"] is None
    assert payload["force"] is False
    # The document itself is JSON only: no configuration values are printed.
    assert "supersecret" not in json.dumps(payload)

    exported = (work / "backup.toml").read_text(encoding="utf-8")
    header, _, body = exported.partition("\n\n")
    assert header.startswith("# novel-downloader configuration export\n")
    assert "# export-format: 1\n" in header + "\n"
    assert re.search(r"^# app-version: \d+\.\d+\.\d+$", header, re.MULTILINE)
    assert re.search(
        r"^# exported-at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        header,
        re.MULTILINE,
    )
    assert body == _secret_config()


def test_export_existing_destination_requires_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.toml").write_text(_secret_config(), encoding="utf-8")
    destination = work / "backup.toml"
    destination.write_text("# previous export\n", encoding="utf-8")
    _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "export", "--out", "backup.toml", "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["status"] == "error"
    assert "already exists" in str(payload["error"])
    assert destination.read_text(encoding="utf-8") == "# previous export\n"
    assert not (work / "backup.toml.bak").exists()


def test_export_force_backs_up_then_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.toml").write_text(_secret_config(workers=5), encoding="utf-8")
    destination = work / "backup.toml"
    destination.write_text("# previous export\n", encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "export", "--out", "backup.toml", "--force", "--json"])

    payload = _json_out(capsys)
    assert payload["status"] == "overwritten"
    assert payload["force"] is True
    assert payload["backup"] == str(work / "backup.toml.bak")
    assert (work / "backup.toml.bak").read_text(
        encoding="utf-8"
    ) == "# previous export\n"
    exported = destination.read_text(encoding="utf-8")
    assert "workers = 5" in exported


@pytest.mark.parametrize("argv", [(), ("--force",)])
def test_export_rejects_directory_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.toml").write_text(_secret_config(), encoding="utf-8")
    destination = work / "backup.toml"
    destination.mkdir()
    (destination / "keep.txt").write_text("data", encoding="utf-8")
    _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "export", "--out", "backup.toml", *argv, "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["status"] == "error"
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "data"


def test_export_without_configuration_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "export", "--out", "backup.toml", "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["source"] is None
    assert payload["status"] == "error"
    assert not (work / "backup.toml").exists()


def test_export_rejects_invalid_source_without_writing_or_leaking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    # The invalid table header makes parsing fail after the secret line.
    (work / "settings.toml").write_text(
        'proxy = "http://user:supersecret@127.0.0.1:7890"\n[general\n',
        encoding="utf-8",
    )
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "export", "--out", "backup.toml"])

    assert excinfo.value.code == 1
    assert not (work / "backup.toml").exists()
    captured = capsys.readouterr()
    assert "supersecret" not in captured.out + captured.err
    assert "supersecret" not in messages.all_text()


def test_export_human_output_explains_credential_risk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.toml").write_text(_secret_config(), encoding="utf-8")
    messages = _capture_ui(monkeypatch)

    _run(["config", "export", "--out", "backup.toml"])

    assert messages.success
    assert (
        t(
            "The exported file may contain credentials "
            "(for example a proxy password). Keep it private."
        )
        in messages.info
    )
    assert "supersecret" not in messages.all_text()


def test_export_write_failure_leaves_no_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.toml").write_text(_secret_config(), encoding="utf-8")
    destination = work / "backup.toml"
    messages = _capture_ui(monkeypatch)

    real_replace = config.os.replace

    def _replace(src: Path | str, dst: Path | str) -> None:
        if Path(dst) == destination:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", _replace)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "export", "--out", "backup.toml"])

    assert excinfo.value.code == 1
    assert not destination.exists()
    assert not list(work.glob("backup.toml.*.tmp"))
    assert messages.error


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_replaces_active_config_and_keeps_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.write_text(_secret_config(workers=2), encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=7), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload = _json_out(capsys)
    assert payload["command"] == "config import"
    assert payload["status"] == "imported"
    assert payload["path"] == str(target)
    assert payload["source"] == str(source)
    assert payload["backup"] == str(work / "settings.toml.bak")
    assert target.read_text(encoding="utf-8") == _secret_config(workers=7)
    assert (work / "settings.toml.bak").read_text(encoding="utf-8") == _secret_config(
        workers=2
    )


def test_import_creates_local_config_when_none_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=4), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload = _json_out(capsys)
    assert payload["path"] == str(work / "settings.toml")
    assert payload["backup"] is None
    assert (work / "settings.toml").read_text(encoding="utf-8") == _secret_config(
        workers=4
    )


def test_import_uses_app_home_when_no_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    home_target = tmp_path / "home" / "settings.toml"
    home_target.write_text(_secret_config(workers=2), encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=9), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source)])

    assert home_target.read_text(encoding="utf-8") == _secret_config(workers=9)
    assert not (work / "settings.toml").exists()
    assert (tmp_path / "home" / "settings.toml.bak").exists()


@pytest.mark.parametrize(
    ("source_text", "expected_fragment"),
    [
        ("[general\nworkers = 1\n", "Invalid TOML"),
        (
            '[general]\nworkers = "many"\nproxy = "http://u:supersecret@h:1"\n',
            "general.workers",
        ),
        ("[tool.other]\nvalue = 1\n", "Not a novel-downloader configuration"),
    ],
)
def test_import_rejects_invalid_source_without_touching_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_text: str,
    expected_fragment: str,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _secret_config(workers=2)
    target.write_text(original, encoding="utf-8")
    source = tmp_path / "candidate.toml"
    source.write_text(source_text, encoding="utf-8")
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "import", "--from", str(source), "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["status"] == "error"
    assert expected_fragment in str(payload["error"])
    assert target.read_text(encoding="utf-8") == original
    assert not (work / "settings.toml.bak").exists()
    captured = capsys.readouterr()
    assert "supersecret" not in captured.out + captured.err
    assert "supersecret" not in messages.all_text()


def test_import_creates_local_toml_over_a_shadowing_json_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    (work / "settings.json").write_text('{"general": {"workers": 2}}', encoding="utf-8")
    home_target = tmp_path / "home" / "settings.toml"
    home_target.write_text(_secret_config(workers=1), encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=4), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source)])

    # A TOML file in the current directory outranks both the local JSON file
    # and the app-home TOML file, so the import actually takes effect.
    assert (work / "settings.toml").read_text(encoding="utf-8") == _secret_config(
        workers=4
    )
    assert home_target.read_text(encoding="utf-8") == _secret_config(workers=1)
    assert (work / "settings.json").read_text(
        encoding="utf-8"
    ) == '{"general": {"workers": 2}}'


def test_import_rejects_directory_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.write_text(_secret_config(), encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.mkdir()
    _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "import", "--from", str(source), "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["status"] == "error"
    assert target.read_text(encoding="utf-8") == _secret_config()


def test_import_rejects_directory_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.mkdir()
    (target / "keep.txt").write_text("data", encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=8), encoding="utf-8")
    _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "import", "--from", str(source), "--json"])

    assert excinfo.value.code == 1
    payload = _json_out(capsys)
    assert payload["status"] == "error"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "data"


def test_import_accepts_a_toml_file_without_toml_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    source = tmp_path / "cfg-backup"
    source.write_text(_secret_config(workers=6), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source)])

    assert (work / "settings.toml").read_text(encoding="utf-8") == _secret_config(
        workers=6
    )


def test_import_accepts_an_exported_file_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    settings = work / "settings.toml"
    settings.write_text(_secret_config(workers=5), encoding="utf-8")
    _capture_ui(monkeypatch)

    _run(["config", "export", "--out", "backup.toml"])
    settings.unlink()
    _run(["config", "import", "--from", "backup.toml"])

    text = settings.read_text(encoding="utf-8")
    assert text.startswith("# novel-downloader configuration export\n")
    assert "workers = 5" in text


def test_import_strips_a_utf8_bom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    source = tmp_path / "backup.toml"
    source.write_bytes(b"\xef\xbb\xbf" + _secret_config().encode("utf-8"))
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source)])

    assert (work / "settings.toml").read_text(encoding="utf-8") == _secret_config()


def test_import_write_failure_keeps_target_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _secret_config(workers=2)
    target.write_text(original, encoding="utf-8")
    source = tmp_path / "backup.toml"
    source.write_text(_secret_config(workers=7), encoding="utf-8")
    messages = _capture_ui(monkeypatch)

    real_replace = config.os.replace

    def _replace(src: Path | str, dst: Path | str) -> None:
        if Path(dst) == target:
            raise OSError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", _replace)

    with pytest.raises(SystemExit) as excinfo:
        _run(["config", "import", "--from", str(source)])

    assert excinfo.value.code == 1
    assert target.read_text(encoding="utf-8") == original
    assert not list(work.glob("settings.toml.*.tmp"))
    assert messages.error
