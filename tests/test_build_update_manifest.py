#!/usr/bin/env python3
"""Tests for scripts/build_update_manifest.py (pure standard library)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_update_manifest.py"
_spec = importlib.util.spec_from_file_location("build_update_manifest", _SCRIPT)
assert _spec and _spec.loader
manifest = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = manifest
_spec.loader.exec_module(manifest)

VERSION = "1.5.0"
TAG = f"v{VERSION}"
REPO = "owner/name"

ASSETS = [
    ("NovelDownloader-setup-1.5.0-x64.exe", "1" * 64, 43259516),
    ("NovelDownloader-1.5.0-windows-x64.zip", "2" * 64, 56364332),
    ("novel-cli-1.5.0-windows-x64.zip", "3" * 64, 56587132),
    ("novel-api-1.5.0-windows-x64.zip", "4" * 64, 57926467),
    ("novel-downloader-1.5.0-android.apk", "5" * 64, 62714163),
    ("SHA256SUMS.txt", "6" * 64, 581),
    ("manifest.json", "7" * 64, 1300),
]


def _release(assets=ASSETS) -> dict[str, object]:
    return {
        "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}",
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"https://github.com/{REPO}/releases/download/{TAG}/{name}"
                ),
                "digest": f"sha256:{digest}",
                "size": size,
            }
            for name, digest, size in assets
        ],
    }


def _build(**kwargs: object) -> dict[str, object]:
    params = {"version": VERSION, "tag": TAG, "repo": REPO}
    params.update(kwargs)
    return manifest.build_manifest(_release(), **params)  # type: ignore[arg-type]


class TestBuildManifest:
    def test_maps_each_asset_onto_a_target_key(self) -> None:
        document = _build()

        assert set(document["platforms"]) == {
            "windows-x64-installer",
            "windows-x64-portable",
            "windows-x64-cli",
            "windows-x64-api",
            "android",
        }
        android = document["platforms"]["android"]
        assert android["name"] == "novel-downloader-1.5.0-android.apk"
        assert android["sha256"] == "5" * 64
        assert android["size"] == 62714163

    def test_control_files_are_not_platform_entries(self) -> None:
        platforms = _build()["platforms"]

        assert "SHA256SUMS.txt" not in json.dumps(platforms)
        assert "manifest.json" not in json.dumps(platforms)

    def test_records_version_and_tag(self) -> None:
        document = _build()

        assert document["version"] == VERSION
        assert document["tag"] == TAG
        assert document["repo"] == REPO
        assert document["schema"] == manifest.SCHEMA_VERSION

    def test_notes_url_is_optional(self) -> None:
        assert "notes_url" not in _build()
        assert _build(notes_url="https://x")["notes_url"] == "https://x"

    def test_rejects_a_release_without_assets(self) -> None:
        with pytest.raises(manifest.ManifestError, match="no assets"):
            manifest.build_manifest({"assets": []}, version=VERSION, tag=TAG, repo=REPO)

    def test_rejects_an_asset_without_a_digest(self) -> None:
        release = _release()
        release["assets"][0]["digest"] = None  # type: ignore[index]

        with pytest.raises(manifest.ManifestError, match="digest"):
            manifest.build_manifest(release, version=VERSION, tag=TAG, repo=REPO)

    def test_rejects_a_non_https_url(self) -> None:
        release = _release()
        release["assets"][0]["browser_download_url"] = "http://x/y"  # type: ignore[index]

        with pytest.raises(manifest.ManifestError, match="https"):
            manifest.build_manifest(release, version=VERSION, tag=TAG, repo=REPO)

    def test_rejects_a_zero_sized_asset(self) -> None:
        release = _release()
        release["assets"][0]["size"] = 0  # type: ignore[index]

        with pytest.raises(manifest.ManifestError, match="size"):
            manifest.build_manifest(release, version=VERSION, tag=TAG, repo=REPO)

    def test_rejects_two_assets_for_the_same_target(self) -> None:
        release = _release(
            ASSETS + [("NovelDownloader-setup-1.5.0-x64-copy.exe", "8" * 64, 100)]
        )

        with pytest.raises(manifest.ManifestError, match="second asset"):
            manifest.build_manifest(release, version=VERSION, tag=TAG, repo=REPO)

    def test_document_without_payload_assets_is_rejected(self) -> None:
        with pytest.raises(manifest.ManifestError, match="no updatable"):
            manifest.build_manifest(
                _release([("SHA256SUMS.txt", "9" * 64, 10)]),
                version=VERSION,
                tag=TAG,
                repo=REPO,
            )


class TestCanonicalBytes:
    def test_is_reproducible_and_sorted(self) -> None:
        first = manifest.manifest_bytes({"b": 1, "a": 2})
        second = manifest.manifest_bytes({"a": 2, "b": 1})

        assert first == second
        assert first.endswith(b"\n")
        assert json.loads(first) == {"a": 2, "b": 1}


class TestDiff:
    def test_identical_documents_have_no_differences(self) -> None:
        assert manifest.diff_manifests(_build(), _build()) == []

    def test_missing_platform_is_reported(self) -> None:
        actual = _build()
        del actual["platforms"]["android"]  # type: ignore[index]

        problems = manifest.diff_manifests(actual, _build())

        assert any("android: missing" in problem for problem in problems)

    def test_unexpected_platform_is_reported(self) -> None:
        actual = _build()
        actual["platforms"]["windows-arm64-portable"] = {"name": "x"}  # type: ignore[index]

        problems = manifest.diff_manifests(actual, _build())

        assert any("windows-arm64-portable: unexpected" in p for p in problems)

    def test_changed_entry_is_reported(self) -> None:
        actual = _build()
        actual["platforms"]["android"]["sha256"] = "0" * 64  # type: ignore[index]

        assert any(
            "android: differs" in p for p in manifest.diff_manifests(actual, _build())
        )

    def test_signature_fields_are_ignored(self) -> None:
        actual = _build()
        actual["signature"] = "AAAA"
        actual["key_id"] = "deadbeef"

        assert manifest.diff_manifests(actual, _build()) == []


class TestSigning:
    def test_round_trip(self) -> None:
        seed, public = manifest.generate_keypair()
        signed = manifest.sign_document(_build(), seed)

        assert signed["key_id"] == manifest.key_id(public)
        assert manifest.verify_document(signed, public)

    def test_tampering_is_detected(self) -> None:
        seed, public = manifest.generate_keypair()
        signed = manifest.sign_document(_build(), seed)
        signed["version"] = "9.9.9"

        assert not manifest.verify_document(signed, public)

    def test_a_different_key_does_not_verify(self) -> None:
        seed, _ = manifest.generate_keypair()
        _, other_public = manifest.generate_keypair()

        assert not manifest.verify_document(
            manifest.sign_document(_build(), seed), other_public
        )

    def test_unsigned_documents_do_not_verify(self) -> None:
        _, public = manifest.generate_keypair()

        assert not manifest.verify_document(_build(), public)

    @pytest.mark.parametrize("seed", ["zz", "", "00" * 31])
    def test_bad_seeds_are_rejected(self, seed: str) -> None:
        with pytest.raises(manifest.ManifestError):
            manifest.sign_document(_build(), seed)


class TestCli:
    def _write_release(self, tmp_path: Path) -> Path:
        path = tmp_path / "release.json"
        path.write_text(json.dumps(_release()), encoding="utf-8")
        return path

    def test_writes_the_manifest(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"

        rc = manifest.main(
            [
                "--release-json",
                str(self._write_release(tmp_path)),
                "--version",
                VERSION,
                "--tag",
                TAG,
                "--repo",
                REPO,
                "--out",
                str(out),
            ]
        )

        assert rc == 0
        assert json.loads(out.read_text(encoding="utf-8"))["version"] == VERSION

    def test_check_passes_for_an_identical_manifest(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"
        release = self._write_release(tmp_path)
        base = [
            "--release-json",
            str(release),
            "--version",
            VERSION,
            "--tag",
            TAG,
            "--repo",
            REPO,
        ]
        assert manifest.main(base + ["--out", str(out)]) == 0

        assert manifest.main(base + ["--check", str(out)]) == 0

    def test_check_fails_for_a_stale_manifest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "latest.json"
        out.write_text('{"schema": 1, "version": "0.0.1", "platforms": {}}')
        release = self._write_release(tmp_path)

        rc = manifest.main(
            [
                "--release-json",
                str(release),
                "--version",
                VERSION,
                "--tag",
                TAG,
                "--repo",
                REPO,
                "--check",
                str(out),
            ]
        )

        assert rc == 1
        assert "stale" in capsys.readouterr().err

    def test_url_must_point_at_the_released_tag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        release = _release()
        release["assets"][0]["browser_download_url"] = (  # type: ignore[index]
            f"https://github.com/{REPO}/releases/download/vOLD/x.exe"
        )
        path = tmp_path / "release.json"
        path.write_text(json.dumps(release), encoding="utf-8")

        rc = manifest.main(
            [
                "--release-json",
                str(path),
                "--version",
                VERSION,
                "--tag",
                TAG,
                "--repo",
                REPO,
                "--out",
                str(tmp_path / "out.json"),
            ]
        )

        assert rc == 1
        assert "expected" in capsys.readouterr().err

    def test_keygen_writes_a_private_seed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = tmp_path / "update.key"

        rc = manifest.main(["--keygen", str(key), "--release-json", "x"])

        assert rc == 0
        seed = key.read_text(encoding="ascii").strip()
        assert len(seed) == 64
        out = capsys.readouterr().out
        assert "public key" in out
        assert "key id" in out

    def test_signing_and_checking_with_a_public_key(self, tmp_path: Path) -> None:
        key = tmp_path / "update.key"
        assert manifest.main(["--keygen", str(key), "--release-json", "x"]) == 0
        seed = key.read_text(encoding="ascii").strip()
        # derive the public key from the seed we just wrote
        from Crypto.Signature import eddsa

        public = (
            eddsa.import_private_key(bytes.fromhex(seed))
            .public_key()
            .export_key(format="raw")
            .hex()
        )

        release = self._write_release(tmp_path)
        out = tmp_path / "latest.json"
        base = [
            "--release-json",
            str(release),
            "--version",
            VERSION,
            "--tag",
            TAG,
            "--repo",
            REPO,
        ]
        assert manifest.main(base + ["--sign-key", str(key), "--out", str(out)]) == 0

        document = json.loads(out.read_text(encoding="utf-8"))
        assert manifest.verify_document(document, public)
        assert manifest.main(base + ["--check", str(out), "--public-key", public]) == 0

    def test_check_rejects_a_bad_signature(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = tmp_path / "update.key"
        assert manifest.main(["--keygen", str(key), "--release-json", "x"]) == 0
        release = self._write_release(tmp_path)
        out = tmp_path / "latest.json"
        base = [
            "--release-json",
            str(release),
            "--version",
            VERSION,
            "--tag",
            TAG,
            "--repo",
            REPO,
        ]
        assert manifest.main(base + ["--sign-key", str(key), "--out", str(out)]) == 0

        document = json.loads(out.read_text(encoding="utf-8"))
        document["version"] = "9.9.9"
        out.write_text(json.dumps(document), encoding="utf-8")

        from Crypto.Signature import eddsa

        public = (
            eddsa.import_private_key(
                bytes.fromhex(key.read_text(encoding="ascii").strip())
            )
            .public_key()
            .export_key(format="raw")
            .hex()
        )

        assert manifest.main(base + ["--check", str(out), "--public-key", public]) == 1
        assert "does not verify" in capsys.readouterr().err
