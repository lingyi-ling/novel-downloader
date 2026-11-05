"""Tests for scripts/audit_release_assets.py.

Every test runs offline in a temporary directory.  Fixtures build the real
asset names shipped by ``scripts/release.py`` and then inject exactly one kind
of contamination at a time, so each problem kind is asserted independently.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_release_assets.py"

VERSION = "1.4.1"
CLI = f"novel-cli-{VERSION}-windows-x64.zip"
API = f"novel-api-{VERSION}-windows-x64.zip"
DESKTOP = f"NovelDownloader-{VERSION}-windows-x64.zip"
SETUP = f"NovelDownloader-setup-{VERSION}-x64.exe"
APK = f"novel-downloader-{VERSION}-android.apk"
APK_VARIANT = f"novel-downloader-{VERSION}-android-release.apk"
SUMS = "SHA256SUMS.txt"
MANIFEST = "manifest.json"

NORMAL_NAMES = (CLI, API, DESKTOP, SETUP, APK, APK_VARIANT, SUMS, MANIFEST)


def _load_script() -> Any:
    """Import a non-package script by path, as tests/test_release_script.py does."""
    spec = importlib.util.spec_from_file_location(
        "audit_release_assets_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


audit = _load_script()


def _write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    """Write a stored (uncompressed) zip so raw-byte markers stay visible."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, data in members:
            archive.writestr(name, data)


def _safe_zip(path: Path, extra: list[tuple[str, bytes]] | None = None) -> None:
    members = [("bundle/data.bin", b"x" * 4096)]
    members.extend(extra or [])
    _write_zip(path, members)


def _write_zip_with_backslash_member(path: Path, member: str, data: bytes) -> None:
    """Write a zip whose member name really contains a backslash.

    ``zipfile.ZipInfo`` normalizes ``\\`` to ``/`` on write, so the stored
    bytes are patched afterwards.  The replacement is the same length, hence
    offsets and CRCs in the central directory remain valid.
    """
    stored = member.replace("\\", "/")
    _write_zip(path, [(stored, data)])
    path.write_bytes(path.read_bytes().replace(stored.encode(), member.encode()))


def _build_normal(directory: Path) -> None:
    _safe_zip(directory / CLI, [("bundle/module.py", b"print('hello')\n")])
    _safe_zip(directory / API)
    _safe_zip(directory / DESKTOP)
    (directory / SETUP).write_bytes(b"\x00" * 4096)
    _safe_zip(directory / APK)
    _safe_zip(directory / APK_VARIANT)
    (directory / SUMS).write_text("deadbeef  file.zip\n", encoding="utf-8")
    (directory / MANIFEST).write_text('{"ok": true}\n', encoding="utf-8")


def _kinds(result: Any) -> list[str]:
    return [problem.kind for problem in result.problems]


def _write_release_json(path: Path, names: list[str]) -> None:
    payload = {"tag_name": f"v{VERSION}", "assets": [{"name": n} for n in names]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_normal_directory_passes(tmp_path: Path) -> None:
    _build_normal(tmp_path)
    result = audit.audit_directory(tmp_path)
    assert result.ok, _kinds(result)
    assert result.scanned == len(NORMAL_NAMES)
    assert result.python_files == 1
    assert result.problems == []


@pytest.mark.parametrize(
    "name",
    [
        CLI,
        API,
        DESKTOP,
        SETUP,
        APK,
        APK_VARIANT,
        f"novel-cli-{VERSION}-linux-x64.zip",
        f"NovelDownloader-{VERSION}-macos-arm64.zip",
        "SHA256SUMS.txt",
        "manifest.json",
    ],
)
def test_allowlist_accepts_real_asset_names(name: str) -> None:
    assert audit.validate_asset_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "notes.zip",
        "../novel-cli-1.4.1-windows-x64.zip",
        "C:novel-cli-1.4.1-windows-x64.zip",
        "novel-cli-1.4.1-windows\\x64.zip",
        "novel-cli-1.4.1-windows-x64.tar.gz",
        "novel-cli-1.4.1-windows-x64.ZIP",
        "novel-cli-1.4.1-windows-x64-é.zip",
        "novel-cli-1.4.1-windows-x64.zip\n",
        "a" * 201 + ".zip",
    ],
)
def test_allowlist_rejects_invalid_asset_names(name: str) -> None:
    assert audit.validate_asset_name(name) is False


def test_unexpected_asset_name_is_reported(tmp_path: Path) -> None:
    _safe_zip(tmp_path / "notes.zip")
    result = audit.audit_directory(tmp_path)
    assert _kinds(result) == ["unexpected-name"]


def test_zero_byte_file(tmp_path: Path) -> None:
    (tmp_path / SUMS).write_bytes(b"")
    result = audit.audit_directory(tmp_path)
    assert _kinds(result) == ["empty-file"]


def test_suspiciously_small_archive(tmp_path: Path) -> None:
    (tmp_path / CLI).write_bytes(b"tiny")
    result = audit.audit_directory(tmp_path)
    assert "suspiciously-small" in _kinds(result)


def test_suspiciously_large_asset(tmp_path: Path) -> None:
    path = tmp_path / MANIFEST
    path.write_bytes(b"{}")
    os.truncate(path, audit.MAX_ASSET_SIZE + 1)
    result = audit.audit_directory(tmp_path)
    assert _kinds(result) == ["suspiciously-large"]


@pytest.mark.parametrize(
    ("member", "kind"),
    [
        ("../evil", "unsafe-member-path"),
        ("/etc/passwd", "unsafe-member-path"),
        ("C:/Windows/evil", "unsafe-member-path"),
        ("dir\\evil", "unsafe-member-path"),
        (".git/config", "forbidden-directory"),
        (".github/workflows/ci.yml", "forbidden-directory"),
        ("pkg/__pycache__/cached.pyc", "forbidden-directory"),
        ("pkg/.pytest_cache/v/cache", "forbidden-directory"),
        ("node_modules/left-pad/index.js", "forbidden-directory"),
        (".env", "forbidden-file"),
        ("credentials.json", "forbidden-file"),
        ("server.pem", "forbidden-file"),
        ("private.key", "forbidden-file"),
        ("release.jks", "forbidden-file"),
        ("release.keystore", "forbidden-file"),
        ("id_rsa", "forbidden-file"),
        ("id_rsa.pub", "forbidden-file"),
        ("app.pdb", "forbidden-file"),
        ("app.js.map", "forbidden-file"),
        ("Foo.dSYM/Contents/Info.plist", "forbidden-file"),
    ],
)
def test_archive_member_is_rejected(tmp_path: Path, member: str, kind: str) -> None:
    if "\\" in member:
        _write_zip_with_backslash_member(tmp_path / CLI, member, b"x" * 4096)
    else:
        _write_zip(tmp_path / CLI, [(member, b"x" * 4096)])
    result = audit.audit_directory(tmp_path)
    assert kind in _kinds(result), _kinds(result)


def test_python_files_are_allowed_and_counted(tmp_path: Path) -> None:
    _write_zip(
        tmp_path / CLI,
        [
            ("bundle/a.py", b"print('a')\n"),
            ("bundle/pkg/b.py", b"print('b')\n"),
            ("bundle/data.bin", b"x" * 4096),
        ],
    )
    result = audit.audit_directory(tmp_path)
    assert result.problems == []
    assert result.python_files == 2


def test_public_ca_bundles_are_allowed(tmp_path: Path) -> None:
    _write_zip(
        tmp_path / CLI,
        [
            ("bundle/_internal/certifi/cacert.pem", b"public ca data"),
            ("assets/chaquopy/cacert.pem", b"public ca data"),
            ("bundle/data.bin", b"x" * 4096),
        ],
    )
    assert audit.audit_directory(tmp_path).problems == []


def test_other_pem_files_are_still_rejected(tmp_path: Path) -> None:
    _write_zip(
        tmp_path / CLI,
        [
            ("bundle/server.pem", b"private"),
            ("bundle/data.bin", b"x" * 4096),
        ],
    )
    result = audit.audit_directory(tmp_path)
    assert "forbidden-file" in _kinds(result)


def test_raw_secret_marker_is_reported_without_echoing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "github_pat_" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34"
    _write_zip(
        tmp_path / CLI,
        [
            ("bundle/blob.bin", b"\x00" * 2048 + token.encode("ascii")),
            ("bundle/data.bin", b"x" * 4096),
        ],
    )

    human_code = audit.main(["--dir", str(tmp_path)])
    human = capsys.readouterr()
    assert human_code == 1
    assert token not in human.out
    assert token not in human.err

    json_code = audit.main(["--dir", str(tmp_path), "--json"])
    machine = capsys.readouterr()
    assert json_code == 1
    payload = json.loads(machine.out)
    assert token not in machine.out
    assert token not in machine.err
    assert any(p["kind"] == "secret-marker" for p in payload["problems"])


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (b"gho_" + b"Z" * 24, "GitHub token pattern"),
        (b"BEGIN OPENSSH PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY"),
    ],
)
def test_marker_crossing_chunk_boundary_is_detected(
    tmp_path: Path, payload: bytes, label: str
) -> None:
    # Start the marker seven bytes before the 1 MiB boundary so no single
    # chunk contains it; only the 96-byte carry-over can complete the match.
    start = audit.RAW_CHUNK_SIZE - 7
    data = b"A" * start + payload + b"B" * 256
    (tmp_path / APK).write_bytes(data)
    result = audit.audit_directory(tmp_path)
    markers = [p for p in result.problems if p.kind == "secret-marker"]
    assert any(label in problem.detail for problem in markers), markers
    assert len(markers) == 1, markers


def test_corrupt_zip_is_unreadable_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / CLI).write_bytes(b"this is not a zip file" * 200)
    result = audit.audit_directory(tmp_path)
    assert "unreadable-archive" in _kinds(result)


def test_archive_member_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "MAX_ARCHIVE_MEMBERS", 1)
    _write_zip(
        tmp_path / CLI,
        [("bundle/a.bin", b"a" * 2048), ("bundle/b.bin", b"b" * 2048)],
    )
    result = audit.audit_directory(tmp_path)
    assert _kinds(result) == ["archive-too-large"]


@pytest.mark.parametrize(
    "marker",
    [
        b"BEGIN OPENSSH PRIVATE KEY",
        b"BEGIN RSA PRIVATE KEY",
        b"github_pat_",
        b"ghp_",
        b"storePassword=",
        b"keyPassword=",
        b"NOVELDL_DEPLOY_PASSWORD",
    ],
)
def test_every_raw_marker_is_detected(tmp_path: Path, marker: bytes) -> None:
    data = b"\x00" * 2048 + marker + b"\x00" * 2048
    (tmp_path / SETUP).write_bytes(data)
    result = audit.audit_directory(tmp_path)
    markers = [p for p in result.problems if p.kind == "secret-marker"]
    assert any(marker.decode("ascii") in p.detail for p in markers)


def test_release_json_missing_and_orphan_classification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _safe_zip(assets / CLI)
    _safe_zip(assets / API)
    payload = tmp_path / "payload.json"
    _write_release_json(payload, [CLI, DESKTOP])

    code = audit.main(["--dir", str(assets), "--release-json", str(payload), "--json"])
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    by_kind = {problem["kind"]: problem["asset"] for problem in report["problems"]}
    assert by_kind["missing-asset"] == DESKTOP
    assert by_kind["orphan-file"] == API


def test_release_json_match_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _safe_zip(assets / CLI)
    payload = tmp_path / "payload.json"
    _write_release_json(payload, [CLI])

    code = audit.main(["--dir", str(assets), "--release-json", str(payload), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True


def test_json_output_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_normal(tmp_path)
    code = audit.main(["--dir", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["scanned"] == len(NORMAL_NAMES)
    assert payload["problems"] == []
    assert payload["python_files"] == 1


def test_missing_directory_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = audit.main(["--dir", str(tmp_path / "missing")])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_100mb_scan_stays_within_time_budget(tmp_path: Path) -> None:
    path = tmp_path / APK
    path.write_bytes(b"")
    os.truncate(path, 100 * 1024 * 1024)
    started = time.perf_counter()
    result = audit.audit_directory(tmp_path)
    elapsed = time.perf_counter() - started
    assert result.scanned == 1
    assert elapsed < 10.0, f"100 MiB scan took {elapsed:.2f}s"


def test_latest_json_is_an_allowed_control_asset(tmp_path: Path) -> None:
    """The updater manifest is published with every release."""
    (tmp_path / "latest.json").write_text(
        '{"schema": 1, "version": "1.5.0", "platforms": {}}', encoding="utf-8"
    )

    result = audit.audit_directory(tmp_path)

    assert result.ok
