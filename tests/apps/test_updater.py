#!/usr/bin/env python3
"""
tests.apps.test_updater
-----------------------

Offline tests for :mod:`novel_downloader.apps.common.updater`.

Every network touch point is monkeypatched: the release payload comes from an
in-memory ``BytesIO``, and mirror probes are replaced by a recording fake.  No
test in this module performs real I/O over the network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common import updater

#: The signing producer lives in ``scripts/`` and is loaded the same way
#: ``release.py`` loads it, so these tests exercise the real build path.
_BUILD_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "build_update_manifest.py"
)
_spec = importlib.util.spec_from_file_location(
    "build_update_manifest_for_updater_tests", _BUILD_SCRIPT
)
assert _spec and _spec.loader
build_manifest = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build_manifest
_spec.loader.exec_module(build_manifest)


def _asset(
    name: str,
    url: str = "https://example.invalid/asset",
    size: int | None = None,
    sha256: str | None = None,
) -> updater.ReleaseAsset:
    return updater.ReleaseAsset(name=name, url=url, size=size, sha256=sha256)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("v2.0", (2, 0)),
        ("V10.0.1", (10, 0, 1)),
        ("1.10.0-rc1", (1, 10, 0)),
        ("1.2.3.4", (1, 2, 3, 4)),
        ("", (0,)),
        ("not-a-version", (0,)),
        ("1.x.3", (1,)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...]) -> None:
    assert updater.parse_version(text) == expected


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("1.3.0", "1.2.3", True),
        ("1.2.3", "1.3.0", False),
        ("1.2.3", "1.2.3", False),
        ("v1.3.0-rc2", "1.2.9", True),
        ("2.0", "1.9.9", True),
        ("1.2.3.0", "1.2.3", False),
        ("1.2.3", "1.2.3.0", False),
        ("banana", "1.0.0", False),
        ("1.0.0", "", True),
    ],
)
def test_is_newer(latest: str, current: str, expected: bool) -> None:
    assert updater.is_newer(latest, current) is expected


# ---------------------------------------------------------------------------
# platform detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "windows-x64"),
        ("Windows", "ARM64", "windows-arm64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
    ],
)
def test_detect_platform(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        updater,
        "platform",
        SimpleNamespace(system=lambda: system, machine=lambda: machine),
    )
    assert updater.detect_platform() == expected


# ---------------------------------------------------------------------------
# asset selection
# ---------------------------------------------------------------------------


def test_select_asset_prefers_windows_installer() -> None:
    assets = [
        _asset("NovelDownloader-1.3.0-windows-x64.zip"),
        _asset("NovelDownloader-setup-1.3.0-x64.exe"),
        _asset("novel-cli-1.3.0-windows-x64.zip"),
        _asset("novel-api-1.3.0-windows-x64.zip"),
    ]
    chosen = updater.select_asset(assets, "windows-x64")
    assert chosen is not None
    assert chosen.name == "NovelDownloader-setup-1.3.0-x64.exe"


def test_select_asset_uses_desktop_zip_when_installer_is_absent() -> None:
    assets = [
        _asset("novel-cli-1.3.0-windows-x64.zip"),
        _asset("NovelDownloader-1.3.0-windows-x64.zip"),
    ]
    chosen = updater.select_asset(assets, "windows-x64")
    assert chosen is not None
    assert chosen.name == "NovelDownloader-1.3.0-windows-x64.zip"


def test_select_asset_matches_linux_and_macos() -> None:
    assets = [
        _asset("NovelDownloader-1.3.0-linux-x64.zip"),
        _asset("NovelDownloader-1.3.0-macos-arm64.zip"),
    ]
    linux = updater.select_asset(assets, "linux-x64")
    mac = updater.select_asset(assets, "macos-arm64")
    assert linux is not None and linux.name.endswith("linux-x64.zip")
    assert mac is not None and mac.name.endswith("macos-arm64.zip")


def test_select_asset_is_case_insensitive() -> None:
    assets = [_asset("noveldownloader-1.3.0-WINDOWS-X64.ZIP")]
    chosen = updater.select_asset(assets, "windows-x64")
    assert chosen is not None
    assert chosen.name == "noveldownloader-1.3.0-WINDOWS-X64.ZIP"


def test_select_asset_picks_the_android_apk() -> None:
    assets = [
        _asset("NovelDownloader-1.3.0-windows-x64.zip"),
        _asset("novel-downloader-1.3.0-android.apk"),
    ]
    chosen = updater.select_asset(assets, "android")
    assert chosen is not None
    assert chosen.name.endswith(".apk")


@pytest.mark.parametrize(
    "name",
    [
        # ``scripts/release.py`` single-APK name.
        "novel-downloader-1.3.0-android.apk",
        # The desktop bundle spelling (``PROJECT_NAME`` capitalised).
        "NovelDownloader-1.3.0-android.apk",
        # ``scripts/release.py`` per-APK name when several APKs are built.
        "novel-downloader-1.3.0-android-app-arm64-v8a-release.apk",
    ],
)
def test_select_asset_matches_real_android_asset_names(name: str) -> None:
    assets = [
        _asset("NovelDownloader-1.3.0-windows-x64.zip"),
        _asset(name),
        _asset("novel-cli-1.3.0-windows-x64.zip"),
    ]
    chosen = updater.select_asset(assets, "android")
    assert chosen is not None
    assert chosen.name == name


def test_select_asset_prefers_the_canonical_android_apk() -> None:
    assets = [
        _asset("novel-downloader-1.3.0-android-app-release.apk"),
        _asset("novel-downloader-1.3.0-android.apk"),
    ]
    chosen = updater.select_asset(assets, "android")
    assert chosen is not None
    assert chosen.name == "novel-downloader-1.3.0-android.apk"


def test_select_asset_ignores_foreign_apks() -> None:
    assets = [_asset("some-other-tool-1.3.0-android.apk")]
    assert updater.select_asset(assets, "android") is None


def test_select_asset_returns_none_when_nothing_matches() -> None:
    assets = [
        _asset("novel-cli-1.3.0-linux-x64.zip"),
        _asset("novel-api-1.3.0-linux-x64.zip"),
    ]
    assert updater.select_asset(assets, "windows-x64") is None
    assert updater.select_asset([], "linux-x64") is None
    assert updater.select_asset(assets, "") is None


# ---------------------------------------------------------------------------
# checksums
# ---------------------------------------------------------------------------


def test_verify_sha256_accepts_a_matching_digest(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"hello world")
    digest = hashlib.sha256(b"hello world").hexdigest()
    assert updater.verify_sha256(path, digest) is True
    # GitHub's ``sha256:<hex>`` form is accepted too.
    assert updater.verify_sha256(path, f"sha256:{digest.upper()}") is True


def test_verify_sha256_rejects_a_wrong_digest(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"hello world")
    assert updater.verify_sha256(path, "0" * 64) is False
    assert updater.verify_sha256(path, "not-a-digest") is False
    assert updater.verify_sha256(path, "") is False


def test_verify_sha256_handles_missing_files(tmp_path: Path) -> None:
    assert updater.verify_sha256(tmp_path / "missing.bin", "0" * 64) is False


def test_verify_sha256_hashes_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    expected = hashlib.sha256(b"").hexdigest()
    assert updater.verify_sha256(path, expected) is True
    assert updater.verify_sha256(path, "0" * 64) is False


# ---------------------------------------------------------------------------
# mirrors
# ---------------------------------------------------------------------------


def test_resolve_mirror_stops_at_the_first_working_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_head(url: str, _timeout: float) -> bool:
        calls.append(url)
        return url.startswith("https://mirror-b/")

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    original = "https://github.com/x/y/releases/download/v1/a.zip"
    result = updater.resolve_mirror(
        original, ["https://mirror-a/", "https://mirror-b/", "https://mirror-c/"]
    )
    assert result == f"https://mirror-b/{original}"
    assert calls == [
        f"https://mirror-a/{original}",
        f"https://mirror-b/{original}",
    ]


def test_resolve_mirror_falls_back_to_the_original_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_head(url: str, _timeout: float) -> bool:
        calls.append(url)
        return False

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    original = "https://github.com/x/y/releases/download/v1/a.zip"
    assert updater.resolve_mirror(original, ["https://mirror-a/"]) == original
    assert len(calls) == 1


def test_resolve_mirror_skips_non_http_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_head(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    original = "https://github.com/x/y/a.zip"
    result = updater.resolve_mirror(original, ["file:///tmp/", "", "https://mirror/"])
    assert result == f"https://mirror/{original}"
    assert calls == [f"https://mirror/{original}"]


def test_resolve_mirror_leaves_non_http_urls_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_head(_url: str, _timeout: float) -> bool:  # pragma: no cover
        raise AssertionError("must not probe a non-HTTP URL")

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    assert updater.resolve_mirror("file:///etc/passwd", ["https://m/"]) == (
        "file:///etc/passwd"
    )


_ORIGINAL = "https://github.com/o/r/releases/download/v1/x.zip"


@pytest.mark.parametrize(
    ("mirror", "expected"),
    [
        # trailing slash: untouched
        ("https://ghproxy.com/", f"https://ghproxy.com/{_ORIGINAL}"),
        # no trailing slash: one is inserted (not glued on)
        ("https://ghproxy.com", f"https://ghproxy.com/{_ORIGINAL}"),
        # already a complete prefix: used verbatim, never doubled
        (f"https://ghproxy.com/{_ORIGINAL}", f"https://ghproxy.com/{_ORIGINAL}"),
        # surrounding whitespace is ignored
        ("  https://ghproxy.com  ", f"https://ghproxy.com/{_ORIGINAL}"),
    ],
)
def test_resolve_mirror_normalises_the_join(
    monkeypatch: pytest.MonkeyPatch, mirror: str, expected: str
) -> None:
    seen: list[str] = []

    def fake_head(url: str, _timeout: float) -> bool:
        seen.append(url)
        return True

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    assert updater.resolve_mirror(_ORIGINAL, [mirror]) == expected
    assert seen == [expected]
    assert "comhttps" not in seen[0]


def test_resolve_mirror_skips_empty_and_blank_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_head(url: str, _timeout: float) -> bool:
        seen.append(url)
        return True

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    result = updater.resolve_mirror(_ORIGINAL, ["", "   ", "https://m"])
    assert result == f"https://m/{_ORIGINAL}"
    assert seen == [f"https://m/{_ORIGINAL}"]


# ---------------------------------------------------------------------------
# downloading (mocked network)
# ---------------------------------------------------------------------------


def _serve(payload: bytes) -> Any:
    """A fake ``urlopen`` returning ``payload`` as a context manager."""

    def open_(*_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO(payload)

    return open_


def test_download_asset_writes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"release bytes"
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    digest = hashlib.sha256(payload).hexdigest()
    out = updater.download_asset(
        "https://example.invalid/a.zip",
        tmp_path / "a.zip",
        expected_sha256=digest,
        root=tmp_path,
    )
    assert out.read_bytes() == payload
    assert not (tmp_path / "a.zip.part").exists()


def test_download_asset_deletes_a_corrupt_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"corrupt"))
    with pytest.raises(ValueError, match="checksum"):
        updater.download_asset(
            "https://example.invalid/a.zip",
            tmp_path / "a.zip",
            expected_sha256="0" * 64,
            root=tmp_path,
        )
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_download_asset_rejects_non_http_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("must not open a non-HTTP URL")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ValueError, match="non-HTTP"):
        updater.download_asset("file:///etc/passwd", tmp_path / "x", root=tmp_path)


def test_download_asset_refuses_paths_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"x"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside" / "a.zip"
    with pytest.raises(ValueError, match="outside"):
        updater.download_asset("https://example.invalid/a.zip", outside, root=allowed)


def test_download_asset_sanitises_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"x"))
    out = updater.download_asset(
        "https://example.invalid/a.zip",
        tmp_path / "we ird:name?.zip",
        root=tmp_path,
    )
    assert re.fullmatch(r"[A-Za-z0-9._-]+", out.name)
    assert out.name == "we_ird_name_.zip"


@pytest.mark.parametrize("name", ["con", "NUL", "COM1", "lpt1", "con.zip", "aux"])
def test_sanitise_filename_avoids_windows_device_names(name: str) -> None:
    """``con`` blocks the writer and ``NUL`` swallows bytes on Windows."""
    cleaned = updater._sanitize_filename(name)
    assert cleaned != name
    assert not cleaned.lower().startswith(("con", "nul", "com1", "lpt1", "aux"))
    assert cleaned.startswith("_")


@pytest.mark.parametrize("name", ["con", "NUL", "COM1"])
def test_download_asset_never_writes_to_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A device-name target must still land as a normal file inside root."""
    payload = b"release bytes"
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    out = updater.download_asset(
        "https://example.invalid/a.zip",
        tmp_path / name,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        root=tmp_path,
    )
    assert out.parent == tmp_path.resolve()
    assert out.is_file()
    assert out.read_bytes() == payload


def test_download_asset_truncates_a_300_character_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"x"))
    out = updater.download_asset(
        "https://example.invalid/a.zip", tmp_path / ("a" * 300 + ".zip"), root=tmp_path
    )
    assert len(out.name) <= updater._MAX_FILENAME
    assert out.is_file()


def test_download_asset_resolves_a_relative_dest_against_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dest="sub/a.zip"`` means ``<root>/sub/a.zip``, not ``<cwd>/sub/a.zip``."""
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"x"))
    out = updater.download_asset(
        "https://example.invalid/a.zip", "sub/a.zip", root=tmp_path
    )
    assert out == (tmp_path / "sub" / "a.zip").resolve()
    assert out.read_bytes() == b"x"


def test_download_asset_still_rejects_a_relative_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("must not open when the path escapes root")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        updater.download_asset(
            "https://example.invalid/a.zip", "../../evil.exe", root=root
        )
    assert not (tmp_path.parent / "evil.exe").exists()


def test_download_asset_caps_the_mirror_probe_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 30 s transfer timeout must not become 30 s per dead mirror."""
    probes: list[float] = []

    def fake_head(_url: str, timeout: float) -> bool:
        probes.append(timeout)
        return False

    monkeypatch.setattr(updater, "_head_ok", fake_head)
    monkeypatch.setattr("urllib.request.urlopen", _serve(b"x"))
    updater.download_asset(
        "https://example.invalid/a.zip",
        tmp_path / "a.zip",
        mirror=["https://m1/", "https://m2/"],
        root=tmp_path,
    )
    assert probes == [
        min(30.0, updater.DEFAULT_TIMEOUT),
        min(30.0, updater.DEFAULT_TIMEOUT),
    ]
    assert updater.DEFAULT_TIMEOUT < 30.0


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------


def _github_payload() -> dict[str, Any]:
    sha = "a" * 64
    return {
        "tag_name": "v9.9.9",
        "html_url": "https://example.invalid/releases/v9.9.9",
        "assets": [
            {
                "name": "NovelDownloader-9.9.9-windows-x64.zip",
                "browser_download_url": "https://example.invalid/dl/desktop.zip",
                "size": 4096,
                "digest": f"sha256:{sha}",
            },
            {
                "name": "novel-cli-9.9.9-windows-x64.zip",
                "browser_download_url": "https://example.invalid/dl/cli.zip",
                "size": 1024,
            },
        ],
    }


def test_parse_manifest_reads_github_payload() -> None:
    info = updater.parse_manifest(
        _github_payload(), current="1.2.3", platform_tag="windows-x64"
    )
    assert info.current == "1.2.3"
    assert info.latest == "9.9.9"
    assert info.available is True
    assert info.notes_url == "https://example.invalid/releases/v9.9.9"
    assert len(info.assets) == 2
    assert info.asset is not None
    assert info.asset.name == "NovelDownloader-9.9.9-windows-x64.zip"
    assert info.asset.size == 4096
    assert info.asset.sha256 == "a" * 64


def test_parse_manifest_reports_no_update_for_the_same_version() -> None:
    payload = _github_payload()
    payload["tag_name"] = "v1.2.3"
    info = updater.parse_manifest(payload, current="1.2.3", platform_tag="windows-x64")
    assert info.available is False
    assert info.latest == "1.2.3"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tag_name": None, "assets": "not-a-list"},
        {"tag_name": 123, "html_url": 42, "assets": [None, 7, "x"]},
        {
            "tag_name": "v1.0.0",
            "assets": [
                {"name": "", "browser_download_url": "https://x"},
                {"name": "no-url.zip"},
                {"name": "a.zip", "size": "big", "digest": "md5:123"},
            ],
        },
    ],
)
def test_parse_manifest_survives_corrupt_payloads(payload: dict[str, Any]) -> None:
    info = updater.parse_manifest(payload, current="1.2.3")
    assert info.available is False
    assert isinstance(info.assets, tuple)
    # Nothing in the payload may crash asset selection.
    updater.select_asset(info.assets, "windows-x64")


def test_parse_manifest_accepts_a_build_manifest() -> None:
    payload = {
        "version": "2.0.0",
        "artifacts": [],
        "assets": [
            {
                "name": "NovelDownloader-2.0.0-linux-x64.zip",
                "url": "https://example.invalid/dl/x.zip",
                "sha256": "b" * 64,
            }
        ],
    }
    info = updater.parse_manifest(payload, current="1.9.9", platform_tag="linux-x64")
    assert info.available is True
    assert info.asset is not None
    assert info.asset.sha256 == "b" * 64


# ---------------------------------------------------------------------------
# webview bridge
# ---------------------------------------------------------------------------


def _fake_response(payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    return io.BytesIO(body)


def test_check_update_exposes_asset_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    monkeypatch.setattr(wa, "_save_prefs", lambda _values: None)
    monkeypatch.setattr(wa, "detect_platform", lambda: "windows-x64")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: _fake_response(_github_payload())
    )

    result = wa.ApiBridge().check_update()

    assert result["available"] is True
    assert result["latest"] == "9.9.9"
    assert result["platform"] == "windows-x64"
    assert result["asset_name"] == "NovelDownloader-9.9.9-windows-x64.zip"
    assert result["asset_size"] == 4096
    assert result["sha256"] == "a" * 64
    assert result["url"] == "https://example.invalid/releases/v9.9.9"


def test_check_update_returns_platform_for_a_corrupt_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    monkeypatch.setattr(wa, "_save_prefs", lambda _values: None)
    monkeypatch.setattr(wa, "detect_platform", lambda: "linux-x64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: _fake_response({"tag_name": "", "assets": "junk"}),
    )

    result = wa.ApiBridge().check_update()

    assert result["available"] is False
    assert result["platform"] == "linux-x64"
    assert result["url"] == wa._RELEASES_PAGE


#: Keys every ``check_update`` answer must carry, whatever the branch.
_UPDATE_KEYS = {"available", "current", "latest", "url", "message"}


def _check_update_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """Run every ``check_update`` branch and return the answers by name."""
    import time

    import novel_downloader.apps.desktop.webview_app as wa

    def payload() -> dict[str, Any]:
        return _github_payload()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("offline")

    cases: dict[str, tuple[dict[str, Any], Any]] = {
        "available": ({}, lambda *_a, **_k: _fake_response(payload())),
        "current": ({}, lambda *_a, **_k: _fake_response({"tag_name": "v0.0.1"})),
        "corrupt": (
            {},
            lambda *_a, **_k: _fake_response({"tag_name": "", "assets": "junk"}),
        ),
        "offline": ({}, boom),
        "throttled": (
            {"last_update_check": str(time.time())},
            lambda *_a, **_k: pytest.fail("throttled branch must not fetch"),
        ),
    }
    results: dict[str, dict[str, Any]] = {}
    for name, (prefs, opener) in cases.items():
        saved: dict[str, Any] = {}
        monkeypatch.setattr(wa, "_load_prefs", lambda p=prefs: dict(p))
        monkeypatch.setattr(wa, "_save_prefs", lambda v, s=saved: s.update(v))
        monkeypatch.setattr(wa, "detect_platform", lambda: "windows-x64")
        monkeypatch.setattr("urllib.request.urlopen", opener)
        results[name] = wa.ApiBridge().check_update()
    return results


def test_check_update_always_returns_the_contract_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every branch keeps the five keys the front end and callers rely on."""
    results = _check_update_branches(monkeypatch)
    for name, result in results.items():
        assert set(result) >= _UPDATE_KEYS, f"{name} branch is missing keys"
        json.dumps(result)  # must stay JSON-serialisable for pywebview
        assert result["available"] in (True, False)
    assert results["available"]["available"] is True
    assert results["available"]["latest"] == "9.9.9"
    assert results["current"]["latest"] == "0.0.1"
    assert results["throttled"]["throttled"] is True
    assert "error" in results["offline"]


def test_check_update_records_the_throttle_before_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline machine must not retry the fetch on every panel open."""
    import novel_downloader.apps.desktop.webview_app as wa

    saved: dict[str, Any] = {}
    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    monkeypatch.setattr(wa, "_save_prefs", lambda v: saved.update(v))
    monkeypatch.setattr(wa, "detect_platform", lambda: "windows-x64")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    )

    wa.ApiBridge().check_update()

    assert "last_update_check" in saved


# ---------------------------------------------------------------------------
# remote check + throttle cache
# ---------------------------------------------------------------------------


class TestReleaseApiUrl:
    def test_builds_the_latest_endpoint(self) -> None:
        assert updater.release_api_url("owner/name") == (
            "https://api.github.com/repos/owner/name/releases/latest"
        )

    def test_tolerates_surrounding_slashes(self) -> None:
        assert "/repos/owner/name/" in updater.release_api_url("/owner/name/")

    @pytest.mark.parametrize("slug", ["", "owner", "a/b/c", "owner name/repo"])
    def test_rejects_bad_slugs(self, slug: str) -> None:
        with pytest.raises(ValueError):
            updater.release_api_url(slug)


class TestFetchLatestRelease:
    def test_returns_the_payload(self) -> None:
        payload = {"tag_name": "v9.9.9", "assets": []}

        assert (
            updater.fetch_latest_release(opener=lambda url, timeout: payload) == payload
        )

    def test_none_on_transport_error(self) -> None:
        def boom(url: str, timeout: float) -> object:
            raise OSError("offline")

        assert updater.fetch_latest_release(opener=boom) is None

    def test_none_when_the_document_is_not_an_object(self) -> None:
        assert updater.fetch_latest_release(opener=lambda url, t: []) is None


class TestCheckForUpdate:
    def test_reports_an_available_update(self) -> None:
        payload = {
            "tag_name": "v9.9.9",
            "html_url": "https://example.invalid/r",
            "assets": [
                {
                    "name": "novel-downloader-9.9.9-android.apk",
                    "browser_download_url": "https://example.invalid/a.apk",
                    "size": 10,
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        }

        result = updater.check_for_update(
            "1.0.0",
            platform_tag="android",
            opener=lambda url, t: payload,
            now=100.0,
        )

        assert result.info is not None
        assert result.info.latest == "9.9.9"
        assert result.available is True
        assert result.error is None
        assert result.checked_at == 100.0

    def test_unreachable_feed_is_reported_not_raised(self) -> None:
        def boom(url: str, timeout: float) -> object:
            raise OSError("offline")

        result = updater.check_for_update("1.0.0", opener=boom)

        assert result.info is None
        assert result.error == "unreachable"
        assert result.available is False


class TestUpdateState:
    def test_round_trip_and_due(self, tmp_path: Path) -> None:
        state = tmp_path / "update.json"
        info = updater.UpdateInfo(
            current="1.0.0",
            latest="1.4.0",
            available=True,
            notes_url=None,
            asset=None,
            assets=(),
        )

        updater.save_update_state(state, info, now=1000.0)

        assert updater.load_update_state(state)["latest"] == "1.4.0"
        assert updater.check_due(state, interval=3600, now=2000.0) is False
        assert updater.check_due(state, interval=3600, now=5000.0) is True

    def test_missing_or_corrupt_state_is_treated_as_due(self, tmp_path: Path) -> None:
        state = tmp_path / "update.json"

        assert updater.check_due(state, now=100.0) is True
        assert updater.load_update_state(state) == {}

        state.write_text("{not json", encoding="utf-8")
        assert updater.load_update_state(state) == {}
        assert updater.check_due(state, now=100.0) is True

    def test_save_never_raises_on_an_unwritable_path(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")

        # ``blocker / "child"`` cannot be a directory: the save must swallow it.
        updater.save_update_state(blocker / "update.json", None, now=1.0)


# ---------------------------------------------------------------------------
# updater manifest
# ---------------------------------------------------------------------------


def _manifest(version: str = "9.9.9", **platforms: Any) -> dict[str, Any]:
    return {
        "schema": updater.MANIFEST_SCHEMA,
        "version": version,
        "tag": f"v{version}",
        "repo": "o/r",
        "platforms": platforms,
    }


def _entry(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": f"https://github.com/o/r/releases/download/v9.9.9/{name}",
        "sha256": "a" * 64,
        "size": 1234,
    }


class TestManifestUrl:
    def test_points_at_the_latest_release(self) -> None:
        assert updater.manifest_url("owner/name") == (
            "https://github.com/owner/name/releases/latest/download/latest.json"
        )

    def test_rejects_a_bad_slug(self) -> None:
        with pytest.raises(ValueError):
            updater.manifest_url("owner")


class TestSelectManifestAsset:
    def test_prefers_the_installer_over_the_portable_archive(self) -> None:
        platforms = {
            "windows-x64-portable": _entry("portable.zip"),
            "windows-x64-installer": _entry("setup.exe"),
        }

        asset = updater.select_manifest_asset(platforms, "windows-x64")

        assert asset is not None
        assert asset.name == "setup.exe"

    def test_falls_back_to_the_portable_archive(self) -> None:
        platforms = {"windows-x64-portable": _entry("portable.zip")}

        asset = updater.select_manifest_asset(platforms, "windows-x64")

        assert asset is not None
        assert asset.name == "portable.zip"

    def test_android_uses_its_own_key(self) -> None:
        platforms = {"android": _entry("app.apk")}

        asset = updater.select_manifest_asset(platforms, "android")

        assert asset is not None
        assert asset.name == "app.apk"

    def test_an_entry_without_a_valid_checksum_is_ignored(self) -> None:
        bad = {**_entry("setup.exe"), "sha256": "not-a-digest"}

        assert (
            updater.select_manifest_asset({"windows-x64-installer": bad}, "windows-x64")
            is None
        )

    def test_a_non_https_url_is_ignored(self) -> None:
        bad = {**_entry("setup.exe"), "url": "http://example.invalid/setup.exe"}

        assert (
            updater.select_manifest_asset({"windows-x64-installer": bad}, "windows-x64")
            is None
        )


class TestParseUpdateManifest:
    def test_reads_version_and_asset(self) -> None:
        info = updater.parse_update_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}),
            current="1.0.0",
            platform_tag="windows-x64",
        )

        assert info.latest == "9.9.9"
        assert info.available is True
        assert info.asset is not None
        assert info.asset.sha256 == "a" * 64

    @pytest.mark.parametrize("schema", [0, 2, None, "1"])
    def test_rejects_an_unknown_schema(self, schema: Any) -> None:
        payload = _manifest()
        payload["schema"] = schema

        with pytest.raises(ValueError, match="schema"):
            updater.parse_update_manifest(payload, current="1.0.0")

    def test_rejects_a_document_without_platforms(self) -> None:
        payload = _manifest()
        payload["platforms"] = {}

        with pytest.raises(ValueError, match="platforms"):
            updater.parse_update_manifest(payload, current="1.0.0")

    def test_an_older_manifest_is_not_available(self) -> None:
        info = updater.parse_update_manifest(
            _manifest(version="0.9.0", **{"windows-x64-installer": _entry("s.exe")}),
            current="1.0.0",
            platform_tag="windows-x64",
        )

        assert info.available is False


class TestCheckForUpdatePrefersTheManifest:
    def test_uses_the_manifest_when_present(self) -> None:
        urls: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            urls.append(url)
            return _manifest(**{"windows-x64-installer": _entry("setup.exe")})

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.source == "manifest"
        assert result.available is True
        assert urls == [updater.manifest_url(updater.DEFAULT_REPO)]

    def test_falls_back_to_the_api_when_the_manifest_is_unusable(self) -> None:
        urls: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            urls.append(url)
            if url.endswith("latest.json"):
                return {"schema": 99}
            return {"tag_name": "v9.9.9", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.source == "api"
        assert result.info is not None
        assert len(urls) == 2

    def test_falls_back_when_the_manifest_is_unreachable(self) -> None:
        def opener(url: str, timeout: float) -> Any:
            if url.endswith("latest.json"):
                raise OSError("offline")
            return {"tag_name": "v9.9.9", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.source == "api"


# ---------------------------------------------------------------------------
# manifest signatures
# ---------------------------------------------------------------------------


def _sign_manifest(document: dict[str, Any], seed: bytes) -> tuple[dict[str, Any], str]:
    """Sign *document* the same way scripts/build_update_manifest.py does."""
    import base64

    from Crypto.Signature import eddsa

    body = {k: v for k, v in document.items() if k not in {"signature", "key_id"}}
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    private = eddsa.import_private_key(seed)
    signature = eddsa.new(private, "rfc8032").sign(payload)
    public = private.public_key().export_key(format="raw").hex()
    key_id = hashlib.sha256(bytes.fromhex(public)).hexdigest()[:16]
    signed = dict(document)
    signed["key_id"] = key_id
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed, public


def _seed() -> bytes:
    return bytes(range(32))


class TestManifestSignature:
    def test_unsigned_is_accepted_while_no_key_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {})

        info = updater.parse_update_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}),
            current="1.0.0",
            platform_tag="windows-x64",
        )

        assert info.latest == "9.9.9"

    def test_a_valid_signature_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed, public = _sign_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}), _seed()
        )
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {signed["key_id"]: public})

        info = updater.parse_update_manifest(
            signed, current="1.0.0", platform_tag="windows-x64"
        )

        assert info.available is True

    def test_an_unsigned_manifest_is_rejected_once_a_key_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public = _sign_manifest(_manifest(), _seed())
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"deadbeef": public})

        with pytest.raises(ValueError, match="not signed"):
            updater.parse_update_manifest(
                _manifest(**{"windows-x64-installer": _entry("setup.exe")}),
                current="1.0.0",
                platform_tag="windows-x64",
            )

    def test_a_tampered_manifest_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed, public = _sign_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}), _seed()
        )
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {signed["key_id"]: public})
        signed["version"] = "0.0.1"

        with pytest.raises(ValueError, match="does not verify"):
            updater.parse_update_manifest(
                signed, current="1.0.0", platform_tag="windows-x64"
            )

    def test_an_unknown_key_id_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed, _ = _sign_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}), _seed()
        )
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {"0000000000000000": "ab" * 32}
        )

        with pytest.raises(ValueError, match="unknown key"):
            updater.parse_update_manifest(
                signed, current="1.0.0", platform_tag="windows-x64"
            )

    def test_a_bad_signature_does_not_fall_back_to_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting or tampering with the manifest must not bypass the key."""
        signed, public = _sign_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}), _seed()
        )
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {signed["key_id"]: public})
        signed["version"] = "0.0.1"  # invalidates the signature
        api_hits: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            if url.endswith("latest.json"):
                return signed
            api_hits.append(url)
            return {"tag_name": "v9.9.9", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.error is not None
        assert result.error.startswith("untrusted-manifest")
        assert result.available is False
        assert api_hits == []

    def test_a_missing_manifest_does_not_fall_back_when_a_key_is_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public = _sign_manifest(
            _manifest(**{"windows-x64-installer": _entry("setup.exe")}), _seed()
        )
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"dead": public})
        api_hits: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            if url.endswith("latest.json"):
                raise OSError("404")
            api_hits.append(url)
            return {"tag_name": "v9.9.9", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.error == "manifest-unavailable"
        assert api_hits == []


# ---------------------------------------------------------------------------
# install shape: portable copies replace their own folder
# ---------------------------------------------------------------------------


def test_select_asset_prefers_the_bundle_for_portable(tmp_path: Path) -> None:
    setup = updater.ReleaseAsset(
        "NovelDownloader-setup-1.0.0-x64.exe",
        "https://example.test/setup.exe",
        None,
        None,
    )
    bundle = updater.ReleaseAsset(
        "NovelDownloader-1.0.0-windows-x64.zip",
        "https://example.test/bundle.zip",
        None,
        None,
    )
    assets = (setup, bundle)

    assert updater.select_asset(assets, "windows-x64").name == setup.name
    assert (
        updater.select_asset(assets, "windows-x64", shape="portable").name
        == bundle.name
    )
    assert (
        updater.select_asset(assets, "windows-x64", shape="installer").name
        == setup.name
    )


def test_manifest_selection_follows_the_install_shape() -> None:
    platforms = {
        "windows-x64-installer": {
            "name": "setup.exe",
            "url": "https://x/setup.exe",
            "sha256": "a" * 64,
            "size": 10,
        },
        "windows-x64-portable": {
            "name": "bundle.zip",
            "url": "https://x/b.zip",
            "sha256": "b" * 64,
            "size": 10,
        },
    }

    installed = updater.select_manifest_asset(platforms, "windows-x64")
    portable = updater.select_manifest_asset(platforms, "windows-x64", shape="portable")

    assert installed is not None and installed.name == "setup.exe"
    assert portable is not None and portable.name == "bundle.zip"


def test_detect_install_shape_for_an_installed_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed = tmp_path / "Programs" / "NovelDownloader" / "NovelDownloader.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"x")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(installed))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert updater.detect_install_shape() == "installer"


def test_detect_install_shape_for_a_portable_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    portable = tmp_path / "Downloads" / "NovelDownloader" / "NovelDownloader.exe"
    portable.parent.mkdir(parents=True)
    portable.write_bytes(b"x")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(portable))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))

    assert updater.detect_install_shape() == "portable"


# ---------------------------------------------------------------------------
# resumable transfers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes, status: int, length: int | None) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = {}
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_download_asset_resumes_from_a_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    final = tmp_path / "asset.bin"
    part = final.with_name("asset.bin.part")
    part.write_bytes(b"hello ")
    requests: list[dict[str, str]] = []

    def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
        requests.append(dict(getattr(request, "headers", {})))
        return _FakeResponse(b"world", 206, 5)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    progress: list[tuple[int, int]] = []
    expected = hashlib.sha256(b"hello world").hexdigest()

    written = updater.download_asset(
        "https://example.test/asset.bin",
        Path("asset.bin"),
        expected_sha256=expected,
        root=tmp_path,
        resume=True,
        progress=lambda got, total: progress.append((got, total)),
    )

    assert written == final
    assert final.read_bytes() == b"hello world"
    assert requests and requests[0].get("Range") == "bytes=6-"
    assert progress[-1] == (11, 11)


def test_download_asset_restarts_when_the_server_ignores_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    final = tmp_path / "asset.bin"
    final.with_name("asset.bin.part").write_bytes(b"stale")

    def fake_urlopen(_request: object, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(b"fresh", 200, 5)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    written = updater.download_asset(
        "https://example.test/asset.bin", Path("asset.bin"), root=tmp_path, resume=True
    )

    assert written == final
    assert final.read_bytes() == b"fresh"


def test_download_asset_refuses_an_oversized_transfer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_urlopen(_request: object, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(b"0123456789", 200, 10)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="larger than"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            root=tmp_path,
            max_bytes=5,
        )

    assert not (tmp_path / "asset.bin").exists()
    assert not (tmp_path / "asset.bin.part").exists()


def test_download_asset_refuses_an_https_to_http_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Downgraded(_FakeResponse):
        def geturl(self) -> str:
            return "http://mirror.test/asset.bin"

    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda _request, timeout=0: Downgraded(b"payload", 200, 7),
    )

    with pytest.raises(ValueError, match="redirected to plain HTTP"):
        updater.download_asset(
            "https://example.test/asset.bin", Path("asset.bin"), root=tmp_path
        )


def test_failed_transfer_removes_the_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(_request: object, timeout: float = 0) -> _FakeResponse:
        raise OSError("connection reset")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)

    with pytest.raises(OSError):
        updater.download_asset(
            "https://example.test/asset.bin", Path("asset.bin"), root=tmp_path
        )

    assert not (tmp_path / "asset.bin.part").exists()


# ---------------------------------------------------------------------------
# signed manifest, end to end: real key file -> producer -> client verifier
# ---------------------------------------------------------------------------

_E2E_VERSION = "1.5.0"
_E2E_TAG = f"v{_E2E_VERSION}"
_E2E_REPO = "owner/name"


def _e2e_release_payload() -> dict[str, Any]:
    """A release payload shaped exactly like the GitHub API's."""
    assets = (
        (f"NovelDownloader-setup-{_E2E_VERSION}-x64.exe", "1", 43259516),
        (f"NovelDownloader-{_E2E_VERSION}-windows-x64.zip", "2", 56364332),
        (f"novel-downloader-{_E2E_VERSION}-android.apk", "5", 62714163),
    )
    return {
        "html_url": f"https://github.com/{_E2E_REPO}/releases/tag/{_E2E_TAG}",
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"https://github.com/{_E2E_REPO}/releases/download/"
                    f"{_E2E_TAG}/{name}"
                ),
                "digest": f"sha256:{token * 64}",
                "size": size,
            }
            for name, token, size in assets
        ],
    }


def _public_hex(seed_hex: str) -> str:
    from Crypto.Signature import eddsa

    private = eddsa.import_private_key(bytes.fromhex(seed_hex))
    return private.public_key().export_key(format="raw").hex()


def _build_signed_manifest(tmp_path: Path) -> tuple[dict[str, Any], str]:
    """Generate a key and sign a manifest through the CLI; return (doc, key)."""
    release = tmp_path / "release.json"
    release.write_text(json.dumps(_e2e_release_payload()), encoding="utf-8")
    key = tmp_path / "update.key"
    assert build_manifest.main(["--keygen", str(key)]) == 0
    out = tmp_path / "latest.json"
    notes_url = f"https://github.com/{_E2E_REPO}/releases/tag/{_E2E_TAG}"
    assert (
        build_manifest.main(
            [
                "--release-json",
                str(release),
                "--version",
                _E2E_VERSION,
                "--tag",
                _E2E_TAG,
                "--repo",
                _E2E_REPO,
                "--notes-url",
                notes_url,
                "--sign-key",
                str(key),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    return json.loads(out.read_text(encoding="utf-8")), _public_hex(
        key.read_text(encoding="ascii").strip()
    )


def _set_top(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        document[field] = value

    return mutate


def _set_platform(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        document["platforms"]["windows-x64-installer"][field] = value

    return mutate


def _delete(field: str) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        del document[field]

    return mutate


def _add_platform(document: dict[str, Any]) -> None:
    document["platforms"]["linux-x64-portable"] = {
        "name": "evil.zip",
        "url": "https://evil.example/evil.zip",
        "sha256": "0" * 64,
        "size": 1,
    }


class TestSignedManifestEndToEnd:
    def test_the_whole_chain_verifies_on_the_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )

        updater.verify_manifest_signature(document)
        info = updater.parse_update_manifest(
            document, current="1.0.0", platform_tag="windows-x64"
        )

        assert info.latest == _E2E_VERSION
        assert info.available is True
        assert info.notes_url == (
            f"https://github.com/{_E2E_REPO}/releases/tag/{_E2E_TAG}"
        )
        assert info.asset is not None
        assert info.asset.name == f"NovelDownloader-setup-{_E2E_VERSION}-x64.exe"
        assert info.asset.sha256 == "1" * 64
        assert info.asset.size == 43259516

    def test_producer_and_client_sign_identical_bytes(self, tmp_path: Path) -> None:
        """The two implementations must agree byte for byte, including UTF-8."""
        document, _ = _build_signed_manifest(tmp_path)
        document["notes_url"] = "https://example.invalid/版本说明"

        assert build_manifest._signing_bytes(document) == (
            updater._manifest_signing_bytes(document)
        )

    def test_signature_covers_canonical_json_not_file_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )

        reordered = {key: document[key] for key in reversed(list(document))}
        reparsed = json.loads(json.dumps(reordered, indent=2, ensure_ascii=False))

        updater.verify_manifest_signature(reparsed)

    @pytest.mark.parametrize(
        "mutate",
        [
            _set_top("version", "9.9.9"),
            _set_top("tag", "v9.9.9"),
            _set_top("repo", "evil/repo"),
            _set_top("schema", 2),
            _set_top("generated_from", "https://evil.example/release"),
            _set_top("notes_url", "https://evil.example/notes"),
            _set_platform("name", "evil.exe"),
            _set_platform("url", "https://evil.example/evil.exe"),
            _set_platform("sha256", "0" * 64),
            _set_platform("size", 1),
            _delete("notes_url"),
            _add_platform,
        ],
        ids=[
            "version",
            "tag",
            "repo",
            "schema",
            "generated_from",
            "notes_url",
            "platform-name",
            "platform-url",
            "platform-sha256",
            "platform-size",
            "drop-notes-url",
            "add-platform",
        ],
    )
    def test_any_content_change_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )
        mutate(document)

        with pytest.raises(ValueError, match="does not verify"):
            updater.verify_manifest_signature(document)

    def test_a_forged_signature_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )
        document["signature"] = base64.b64encode(bytes(64)).decode("ascii")

        with pytest.raises(ValueError, match="does not verify"):
            updater.verify_manifest_signature(document)

    def test_a_signature_by_another_key_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, _ = _build_signed_manifest(tmp_path)
        _, other_public = build_manifest.generate_keypair()
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: other_public}
        )

        with pytest.raises(ValueError, match="does not verify"):
            updater.verify_manifest_signature(document)

    def test_an_unknown_key_id_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"0" * 16: public})

        with pytest.raises(ValueError, match="unknown key"):
            updater.verify_manifest_signature(document)

    @pytest.mark.parametrize("field", ["signature", "key_id"])
    def test_a_missing_signature_field_is_rejected_once_a_key_is_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )
        _delete(field)(document)

        with pytest.raises(ValueError, match="not signed"):
            updater.verify_manifest_signature(document)

    def test_an_unsigned_manifest_is_still_accepted_while_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-signing behaviour survives until a key is actually trusted."""
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {})
        document, _ = _build_signed_manifest(tmp_path)
        document.pop("signature")
        document.pop("key_id")

        info = updater.parse_update_manifest(
            document, current="1.0.0", platform_tag="windows-x64"
        )

        assert info.available is True

    def test_check_for_update_uses_a_signed_manifest_without_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )
        api_hits: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            if url.endswith("latest.json"):
                return document
            api_hits.append(url)
            return {"tag_name": f"v{_E2E_VERSION}", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.source == "manifest"
        assert result.error is None
        assert result.available is True
        assert api_hits == []

    def test_a_tampered_manifest_never_reaches_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document, public = _build_signed_manifest(tmp_path)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {document["key_id"]: public}
        )
        document["platforms"]["windows-x64-installer"]["sha256"] = "0" * 64
        api_hits: list[str] = []

        def opener(url: str, timeout: float) -> Any:
            if url.endswith("latest.json"):
                return document
            api_hits.append(url)
            return {"tag_name": f"v{_E2E_VERSION}", "assets": []}

        result = updater.check_for_update(
            "1.0.0", platform_tag="windows-x64", opener=opener
        )

        assert result.error is not None
        assert result.error.startswith("untrusted-manifest")
        assert result.available is False
        assert api_hits == []
