"""Static and unit-level tests for the packaging / release chain.

These tests intentionally avoid running PyInstaller or ISCC: they assert the
*contracts* between the pieces of the release pipeline —

* ``packaging/_spec_common.py`` exclusion guards (tk, dev tooling, the
  Android-only ``apps.embedded`` adapter, the desktop UI kept out of the
  console bundles),
* the Inno Setup script's uninstall semantics and version/filename chain
  versus ``scripts/build_desktop.py``,
* ``.github/workflows/build.yml`` versus ``android/app/build.gradle.kts``
  (JDK, compileSdk, build-tools, Chaquopy Python),
* ``scripts/build_release.py`` archive naming versus the download patterns
  in ``bin/install.ps1``, plus its zip verification / summary-table /
  checksum behaviour,
* the exit-code semantics of the three build wrappers.

If one of these tests fails, a refactor broke a cross-file contract that no
single file can see on its own.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
SCRIPTS = ROOT / "scripts"
SRC_PACKAGE = ROOT / "src" / "novel_downloader"
ANDROID_GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
INSTALL_PS1 = ROOT / "bin" / "install.ps1"
ISS = PACKAGING / "noveldl_installer.iss"

_SPEC_COMMON = PACKAGING / "_spec_common.py"
_CLI_SPEC = PACKAGING / "novel_cli.spec"
_API_SPEC = PACKAGING / "novel_api.spec"
_DESKTOP_SPEC = PACKAGING / "novel_desktop.spec"


def _load_script(path: Path, name: str) -> Any:
    """Import a non-package script file (scripts/, packaging/) by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolves string annotations via
    # sys.modules[cls.__module__] and crashes on an unregistered module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _src_files(*subtrees: str) -> list[Path]:
    """All ``.py`` files under ``src/novel_downloader/<subtree>``, no caches."""
    files: list[Path] = []
    for subtree in subtrees:
        base = SRC_PACKAGE / subtree
        files.extend(
            path
            for path in sorted(base.rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return files


@pytest.fixture(scope="module")
def spec_common() -> Any:
    return _load_script(_SPEC_COMMON, "audit_spec_common")


@pytest.fixture(scope="module")
def build_release() -> Any:
    return _load_script(SCRIPTS / "build_release.py", "audit_build_release")


@pytest.fixture(scope="module")
def build_desktop() -> Any:
    return _load_script(SCRIPTS / "build_desktop.py", "audit_build_desktop")


@pytest.fixture(scope="module")
def build_cli() -> Any:
    return _load_script(SCRIPTS / "build_cli.py", "audit_build_cli")


# ---------------------------------------------------------------------------
# 1. Spec exclusion guards
# ---------------------------------------------------------------------------


class TestSpecExcludes:
    """The EXCLUDES contract shared by the three PyInstaller specs."""

    def test_dev_and_tk_excluded(self, spec_common: Any) -> None:
        for module in ("tkinter", "_tkinter", "pytest", "mypy", "ruff"):
            assert module in spec_common.EXCLUDES, f"{module} must stay excluded"

    def test_android_embedded_adapter_excluded(self, spec_common: Any) -> None:
        # apps.embedded is loaded by name from Kotlin (Chaquopy); it must not
        # ride along in any PyInstaller bundle.
        assert "novel_downloader.apps.embedded" in spec_common.EXCLUDES

    def test_shared_excludes_never_drop_the_desktop_app(self, spec_common: Any) -> None:
        # The desktop spec consumes the same EXCLUDES tuple, so listing
        # apps.desktop there would strip the desktop bundle of its own app.
        assert "novel_downloader.apps.desktop" not in spec_common.EXCLUDES
        assert spec_common.CONSOLE_EXCLUDES == ("novel_downloader.apps.desktop",)

    def test_dynamic_trees_cover_the_runtime_plugins(self, spec_common: Any) -> None:
        trees = dict(spec_common.DYNAMIC_TREES)
        assert "plugins/sites" in trees
        assert "plugins/processors" in trees
        assert "plugins/rule" in trees
        assert "apps/music/providers/_vendor/musicdl" in trees
        # No console/desktop spec forces the other app's modules in.
        assert "apps/desktop" not in trees
        assert "apps/embedded" not in trees

    def test_hidden_imports_never_pull_foreign_apps(self, spec_common: Any) -> None:
        for module in spec_common.EXTRA_HIDDEN_IMPORTS:
            assert not module.startswith("novel_downloader.apps.desktop")
            assert not module.startswith("novel_downloader.apps.embedded")

    def test_console_specs_use_the_desktop_guard(self) -> None:
        for spec_path in (_CLI_SPEC, _API_SPEC):
            text = spec_path.read_text(encoding="utf-8")
            assert "CONSOLE_EXCLUDES" in text
            assert "excludes=[*EXCLUDES, *CONSOLE_EXCLUDES]" in text

    def test_desktop_spec_keeps_plain_excludes(self) -> None:
        text = _DESKTOP_SPEC.read_text(encoding="utf-8")
        assert "excludes=list(EXCLUDES)" in text
        assert "CONSOLE_EXCLUDES" not in text

    def test_entry_scripts_exist(self) -> None:
        for name in ("cli_entry.py", "api_entry.py", "desktop_entry.py"):
            assert (PACKAGING / name).is_file(), f"spec entry script missing: {name}"


class TestNoCrossAppImports:
    """
    The exclusion guards are only valid while no code crosses app boundaries.

    These tests are the tripwire: adding an import from ``apps.cli`` /
    ``apps.api`` into the desktop app (or the Android adapter into any
    PyInstaller-reachable app) must fail here, pointing at the spec excludes.
    """

    @pytest.mark.parametrize("path", _src_files("apps/cli", "apps/api"))
    def test_console_apps_do_not_import_desktop_or_embedded(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "apps.desktop" not in text, f"{path} imports the desktop app"
        assert "apps.embedded" not in text, f"{path} imports the Android adapter"
        assert "..desktop" not in text, f"{path} uses a relative desktop import"
        assert "..embedded" not in text, f"{path} uses a relative embedded import"

    @pytest.mark.parametrize("path", _src_files("apps/desktop"))
    def test_desktop_app_does_not_import_console_or_embedded(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "apps.api" not in text, f"{path} imports the API app"
        assert "apps.cli" not in text, f"{path} imports the CLI app"
        assert "apps.embedded" not in text, f"{path} imports the Android adapter"

    def test_android_gets_the_source_tree_via_chaquopy(self) -> None:
        gradle = ANDROID_GRADLE.read_text(encoding="utf-8")
        assert 'id("com.chaquo.python")' in gradle
        assert 'srcDir("../../src")' in gradle


# ---------------------------------------------------------------------------
# 2. Inno Setup installer contract
# ---------------------------------------------------------------------------


class TestInnoInstaller:
    """Uninstall semantics, paths and the version/filename chain of the iss."""

    def test_iss_exists(self) -> None:
        assert ISS.is_file()

    def test_uninstall_preserves_user_data(self) -> None:
        """No [UninstallDelete]: nothing outside {app} is ever removed."""
        text = ISS.read_text(encoding="utf-8")
        assert "[UninstallDelete]" not in text
        assert "UninstallDelete" not in text
        assert "[UninstallRun]" in text  # section exists and stays empty

    def test_install_dir_is_not_the_app_home(self) -> None:
        # The app home (%LOCALAPPDATA%\novel_downloader via platformdirs) is
        # a sibling of the install dir, never inside it.
        text = ISS.read_text(encoding="utf-8")
        assert r"DefaultDirName={localappdata}\Programs\NovelDownloader" in text
        assert r"{localappdata}\novel_downloader" not in text

    def test_desktop_app_chdirs_to_app_home(self) -> None:
        # main.py chdirs into the app home so ./downloads and settings.toml
        # never land inside the (uninstallable) program directory.
        main_py = (SRC_PACKAGE / "apps" / "desktop" / "main.py").read_text(
            encoding="utf-8"
        )
        assert "app_home()" in main_py
        assert "os.chdir(home)" in main_py

    def test_version_macro_has_safe_default(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        assert "#ifndef AppVersion" in text
        assert '#define AppVersion "0.0.0"' in text

    def test_output_filename_matches_build_desktop(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        match = re.search(r"OutputBaseFilename=(\S+)-\{#AppVersion\}-(\S+)", text)
        assert match is not None, "OutputBaseFilename must embed {#AppVersion}"
        prefix, suffix = match.group(1), match.group(2)
        assert prefix == "NovelDownloader-setup"
        assert suffix == "x64"

        desktop_src = (SCRIPTS / "build_desktop.py").read_text(encoding="utf-8")
        expected = "NovelDownloader-setup-{version}-x64.exe"
        assert expected in desktop_src, (
            "build_desktop.py must expect the iss OutputBaseFilename "
            f"({prefix}-<version>-{suffix})"
        )

    def test_iss_paths_point_at_the_desktop_dist(self) -> None:
        """Source/OutputDir resolve to the same dist tree build_desktop makes."""
        text = ISS.read_text(encoding="utf-8")
        source = re.search(r'Source:\s*"([^"]+)"', text)
        out_dir = re.search(r"OutputDir=(\S+)", text)
        assert source is not None and out_dir is not None

        # The iss uses Windows-style relative paths; normalise them so the
        # test also works under a POSIX checkout.
        src_dir = PureWindowsPath(source.group(1)).parent  # ..\dist\NovelDownloader
        assert list(src_dir.parts) == ["..", "dist", "NovelDownloader"]
        assert (
            PACKAGING.joinpath(*src_dir.parts).resolve()
            == (ROOT / "dist" / "NovelDownloader").resolve()
        )

        out_parts = list(PureWindowsPath(out_dir.group(1)).parts)
        assert out_parts == ["..", "dist"]
        assert PACKAGING.joinpath(*out_parts).resolve() == (ROOT / "dist").resolve()


# ---------------------------------------------------------------------------
# 3. GitHub Actions workflow vs the Android build files
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_jobs() -> dict[str, Any]:
    """Parse .github/workflows/build.yml into its jobs mapping."""
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(BUILD_YML.read_text(encoding="utf-8"))["jobs"]


class TestBuildWorkflow:
    """build.yml must stay in sync with android/app/build.gradle.kts."""

    def test_android_jdk_matches_gradle(self, workflow_jobs: dict[str, Any]) -> None:
        gradle = ANDROID_GRADLE.read_text(encoding="utf-8")
        assert "VERSION_17" in gradle and 'jvmTarget = "17"' in gradle

        android = workflow_jobs["android-apk"]["steps"]
        java = next(s for s in android if "setup-java" in str(s.get("uses", "")))
        assert java["with"]["java-version"] == "17"

    def test_android_sdk_matches_compile_sdk(
        self, workflow_jobs: dict[str, Any]
    ) -> None:
        gradle = ANDROID_GRADLE.read_text(encoding="utf-8")
        assert "compileSdk = 35" in gradle

        android = workflow_jobs["android-apk"]["steps"]
        sdk = next(s for s in android if "setup-android" in str(s.get("uses", "")))
        packages = sdk["with"]["packages"]
        assert "platforms;android-35" in packages
        assert "build-tools;35.0.0" in packages
        assert "build-tools;34.0.0" in packages  # AGP 8.6.1 default

    def test_android_python_matches_chaquopy(
        self, workflow_jobs: dict[str, Any]
    ) -> None:
        gradle = ANDROID_GRADLE.read_text(encoding="utf-8")
        assert 'version = "3.12"' in gradle

        android = workflow_jobs["android-apk"]["steps"]
        python = next(s for s in android if "setup-python" in str(s.get("uses", "")))
        assert python["with"]["python-version"] == "3.12"

    def test_artifact_names(self, workflow_jobs: dict[str, Any]) -> None:
        steps = workflow_jobs["python-bundles"]["steps"]
        names = {
            step["with"]["name"]
            for step in steps
            if "upload-artifact" in str(step.get("uses", ""))
        }
        assert "novel-cli-windows-x64" in names
        assert "novel-api-windows-x64" in names
        assert "novel-desktop-windows-x64" in {
            step["with"]["name"]
            for step in workflow_jobs["desktop-bundle"]["steps"]
            if "upload-artifact" in str(step.get("uses", ""))
        }

    def test_desktop_job_relys_on_iscc_skip(
        self, workflow_jobs: dict[str, Any]
    ) -> None:
        # The runner has no Inno Setup: build_desktop.py must be invoked in a
        # mode that skips the installer instead of failing the job.
        run = next(
            step
            for step in workflow_jobs["desktop-bundle"]["steps"]
            if step.get("name", "").startswith("Build NovelDownloader")
        )
        command = run["run"]
        assert "build_desktop.py" in command
        assert "iscc" not in command.lower()


# ---------------------------------------------------------------------------
# 4. build_release.py: naming, verification, summary, manifest
# ---------------------------------------------------------------------------


class TestReleaseArchiveVerification:
    """Zips are CRC-checked and layout-checked before being hashed."""

    def _make_bundle(self, tmp_path: Path, name: str) -> Path:
        bundle = tmp_path / name
        (bundle / "_internal").mkdir(parents=True)
        (bundle / f"{name}.exe").write_bytes(b"MZ launcher")
        (bundle / "_internal" / "data.bin").write_bytes(b"payload" * 100)
        return bundle

    def test_verify_archive_accepts_a_valid_bundle(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        bundle = self._make_bundle(tmp_path, "novel-cli")
        archive = tmp_path / "out" / "novel-cli.zip"
        build_release._archive_bundle(bundle, archive)
        build_release._verify_archive(archive, "novel-cli")  # must not raise

    def test_verify_archive_rejects_truncated_zip(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        bundle = self._make_bundle(tmp_path, "novel-cli")
        archive = tmp_path / "novel-cli.zip"
        build_release._archive_bundle(bundle, archive)
        data = archive.read_bytes()
        archive.write_bytes(data[: len(data) // 2])
        with pytest.raises(RuntimeError, match="not a readable zip"):
            build_release._verify_archive(archive, "novel-cli")

    def test_verify_archive_rejects_corrupt_entry(
        self, build_release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = self._make_bundle(tmp_path, "novel-cli")
        archive = tmp_path / "novel-cli.zip"
        build_release._archive_bundle(bundle, archive)
        monkeypatch.setattr(
            zipfile.ZipFile, "testzip", lambda self: "evil.py", raising=True
        )
        with pytest.raises(RuntimeError, match="corrupt zip entry"):
            build_release._verify_archive(archive, "novel-cli")

    def test_verify_archive_rejects_wrong_layout(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        wrong = tmp_path / "other"
        wrong.mkdir()
        (wrong / "novel-cli.exe").write_bytes(b"MZ")
        archive = tmp_path / "novel-cli.zip"
        build_release._archive_bundle(wrong, archive)
        with pytest.raises(RuntimeError, match="no top-level 'novel-cli/'"):
            build_release._verify_archive(archive, "novel-cli")

    def test_verify_archive_requires_the_launcher(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        bundle = tmp_path / "novel-cli"
        bundle.mkdir()
        (bundle / "_internal").mkdir()
        archive = tmp_path / "novel-cli.zip"
        build_release._archive_bundle(bundle, archive)
        with pytest.raises(RuntimeError, match="does not contain"):
            build_release._verify_archive(archive, "novel-cli")

    def test_archive_bundle_keeps_top_level_folder(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        bundle = self._make_bundle(tmp_path, "novel-api")
        archive = tmp_path / "rel" / "novel-api.zip"
        build_release._archive_bundle(bundle, archive)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert any(name.startswith("novel-api/") for name in names)
        assert "novel-api/novel-api.exe" in names


class TestReleaseHelpers:
    """Pure helper behaviour of build_release.py."""

    def test_print_table_with_no_artifacts_does_not_crash(
        self, build_release: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Regression: max(len(header), *()) raised TypeError when every
        # requested target failed.
        build_release._print_table([])
        output = capsys.readouterr().out
        assert "Target" in output

    def test_print_table_with_artifacts(
        self, build_release: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        build_release._print_table(
            [
                {
                    "target": "cli",
                    "file": "novel-cli-1.2.0-windows-x64.zip",
                    "size": 1234,
                    "sha256": "0" * 64,
                }
            ]
        )
        output = capsys.readouterr().out
        assert "novel-cli-1.2.0-windows-x64.zip" in output

    def test_checksums_empty_releases_no_lone_newline(
        self, build_release: Any, tmp_path: Path
    ) -> None:
        path = build_release._write_checksums(tmp_path, [])
        # verify.ps1 treats a checksum file without entries as an error; a
        # blank-only file would be indistinguishable from one.
        assert path.read_text(encoding="utf-8") == ""

    def test_checksums_format(self, build_release: Any, tmp_path: Path) -> None:
        path = build_release._write_checksums(
            tmp_path,
            [{"sha256": "a" * 64, "file": "x.zip"}],
        )
        assert path.read_text(encoding="utf-8") == f"{'a' * 64}  x.zip\n"

    def test_manifest_fields(self, build_release: Any, tmp_path: Path) -> None:
        path = build_release._write_manifest(
            tmp_path,
            version="1.2.0",
            platform_tag="windows-x64",
            requested=["cli"],
            artifacts=[{"target": "cli", "file": "x.zip", "sha256": "b" * 64}],
            failures=[{"target": "desktop", "message": "boom"}],
            created_at="2026-01-01T00:00:00Z",
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "name",
            "version",
            "platform",
            "created_at",
            "python",
            "pyinstaller",
            "targets",
            "artifacts",
            "failures",
            "checksums_file",
        ):
            assert key in manifest, f"manifest.json missing '{key}'"
        assert manifest["checksums_file"] == "SHA256SUMS.txt"
        assert manifest["failures"] == [{"target": "desktop", "message": "boom"}]

    def test_platform_tag_shape(self, build_release: Any) -> None:
        tag = build_release._platform_tag()
        assert re.fullmatch(r"[a-z0-9]+-[a-z0-9]+", tag), tag
        assert "-" in tag

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("all", ["cli", "api", "desktop"]),
            ("cli", ["cli"]),
            ("cli,api", ["cli", "api"]),
            ("api, cli", ["api", "cli"]),
            ("cli,cli,api", ["cli", "api"]),
        ],
    )
    def test_parse_targets(
        self, build_release: Any, value: str, expected: list[str]
    ) -> None:
        assert build_release._parse_targets(value) == expected

    @pytest.mark.parametrize("value", ["cli,desktop-app", "DESKTOP,unknown", "bogus"])
    def test_parse_targets_rejects_unknown(
        self, build_release: Any, value: str
    ) -> None:
        # NOTE: an empty --targets value intentionally selects every target.
        with pytest.raises(argparse.ArgumentTypeError):
            build_release._parse_targets(value)

    def test_release_targets_cover_all_three_bundles(self, build_release: Any) -> None:
        assert set(build_release.TARGETS) == {"cli", "api", "desktop"}
        assert build_release.TARGETS["desktop"].build is not None


class TestReleaseAssetNamesMatchInstaller:
    """bin/install.ps1 downloads release assets by regex; keep them aligned."""

    def _installer_patterns(self) -> dict[str, re.Pattern[str]]:
        """Extract the ``-match '<regex>'`` asset filters from install.ps1."""
        text = INSTALL_PS1.read_text(encoding="utf-8")
        patterns: dict[str, re.Pattern[str]] = {}
        for raw in re.findall(r"-match\s+'([^']+)'", text):
            if "novel-cli" in raw:
                patterns.setdefault("cli", re.compile(raw))
            elif "NovelDownloader" in raw:
                patterns.setdefault("desktop", re.compile(raw))
        return patterns

    def test_installer_patterns_exist(self) -> None:
        patterns = self._installer_patterns()
        assert "cli" in patterns, "install.ps1 lost the novel-cli asset filter"
        assert "desktop" in patterns, "install.ps1 lost the desktop asset filter"

    def _asset_name(self, build_release: Any, key: str) -> str:
        spec = build_release.TARGETS[key]
        return f"{spec.bundle}-1.2.0-windows-x64.zip"

    def test_cli_zip_matches_installer_regex(self, build_release: Any) -> None:
        regex = self._installer_patterns()["cli"]
        assert regex.fullmatch(self._asset_name(build_release, "cli"))
        # Neither the API nor the desktop zip may satisfy the CLI filter:
        # install.ps1 picks $assets[0] from whatever matches it.
        assert not regex.fullmatch(self._asset_name(build_release, "api"))
        assert not regex.fullmatch(self._asset_name(build_release, "desktop"))

    def test_desktop_zip_matches_installer_regex(self, build_release: Any) -> None:
        regex = self._installer_patterns()["desktop"]
        assert regex.fullmatch(self._asset_name(build_release, "desktop"))
        assert not regex.fullmatch(self._asset_name(build_release, "cli"))
        assert not regex.fullmatch(self._asset_name(build_release, "api"))

    def test_windows_pattern_rejects_other_platforms(self, build_release: Any) -> None:
        regex = self._installer_patterns()["cli"]
        assert not regex.fullmatch("novel-cli-1.2.0-macos-arm64.zip")
        assert not regex.fullmatch("novel-cli-1.2.0-linux-x64.zip")


# ---------------------------------------------------------------------------
# 5. Exit-code semantics of the build wrappers
# ---------------------------------------------------------------------------


class TestBuildCliExitCodes:
    def test_targets_and_specs_exist(self, build_cli: Any) -> None:
        assert set(build_cli.TARGETS) == {"cli", "api"}
        for name, spec_rel in build_cli.TARGETS.values():
            assert (ROOT / spec_rel).is_file(), f"spec missing for {name}"

    def test_success_returns_zero(
        self, build_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = build_cli.BuildResult(target="cli", name="novel-cli", ok=True)
        monkeypatch.setattr(build_cli, "build", lambda target: result)
        assert build_cli.main(["--target", "cli"]) == 0

    def test_failure_returns_three(
        self, build_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = build_cli.BuildResult(
            target="cli", name="novel-cli", ok=False, message="broken"
        )
        monkeypatch.setattr(build_cli, "build", lambda target: result)
        assert build_cli.main(["--target", "cli"]) == 3

    def test_site_searcher_floor_is_realistic(self, build_cli: Any) -> None:
        # The floor must stay below the actual number of site plugins so the
        # post-build validation cannot pass with dropped sites.
        searchers = list((SRC_PACKAGE / "plugins" / "sites").glob("*/searcher.py"))
        assert len(searchers) >= build_cli.MIN_SITE_SEARCHERS
        assert build_cli.MIN_SITE_SEARCHERS >= 1


class TestBuildDesktopExitCodes:
    def _ok_result(self, build_desktop: Any) -> Any:
        result = build_desktop.BuildResult(ok=True)
        result.exe = None
        return result

    def test_success_returns_zero(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_desktop, "build", lambda **kwargs: self._ok_result(build_desktop)
        )
        monkeypatch.setattr(
            build_desktop, "_maybe_build_installer", lambda root: (None, None)
        )
        assert build_desktop.main([]) == 0

    def test_validate_failure_returns_four(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = build_desktop.BuildResult(ok=False, message="bad bundle")
        result.exe = None
        monkeypatch.setattr(build_desktop, "build", lambda **kwargs: result)
        assert build_desktop.main([]) == 4

    def test_prepare_failure_returns_two(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = build_desktop.BuildResult(
            ok=False, stage="prepare", message="no toolchain"
        )
        result.exe = None
        monkeypatch.setattr(build_desktop, "build", lambda **kwargs: result)
        assert build_desktop.main([]) == 2

    def test_pyinstaller_failure_returns_three(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = build_desktop.BuildResult(
            ok=False, stage="pyinstaller", message="boom"
        )
        result.exe = None
        monkeypatch.setattr(build_desktop, "build", lambda **kwargs: result)
        assert build_desktop.main([]) == 3

    def test_installer_compilation_failure_returns_five(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: a failing ISCC compilation used to exit 0.
        monkeypatch.setattr(
            build_desktop, "build", lambda **kwargs: self._ok_result(build_desktop)
        )
        monkeypatch.setattr(
            build_desktop,
            "_maybe_build_installer",
            lambda root: (None, "installer compilation exited with status 1"),
        )
        assert build_desktop.main([]) == 5

    def test_smoke_flag_wires_into_validation(self) -> None:
        source = (SCRIPTS / "build_desktop.py").read_text(encoding="utf-8")
        assert '"--smoke"' in source
        assert "smoke_seconds=args.smoke_seconds if args.smoke else 0.0" in source
        assert "start-up smoke test" in source


class TestInstallerStep:
    """Unit behaviour of build_desktop._maybe_build_installer / _app_version."""

    def test_app_version_reads_the_packaged_source(self, build_desktop: Any) -> None:
        init_py = (SRC_PACKAGE / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
        assert match is not None
        assert build_desktop._app_version(ROOT) == match.group(1)
        assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1))

    def test_missing_iscc_is_a_warning_not_an_error(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_desktop, "_find_iscc", lambda: None)
        installer, error = build_desktop._maybe_build_installer(ROOT)
        assert installer is None
        assert error is None

    def test_failing_iscc_reports_an_error(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path
        (root / "packaging").mkdir()
        iss = root / "packaging" / "noveldl_installer.iss"
        iss.write_text("; stub", encoding="utf-8")
        monkeypatch.setattr(build_desktop, "_find_iscc", lambda: "ISCC.exe")
        monkeypatch.setattr(build_desktop, "_app_version", lambda root: "9.9.9")
        fake_subprocess = types.SimpleNamespace(
            run=lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1)
        )
        monkeypatch.setattr(build_desktop, "subprocess", fake_subprocess)
        installer, error = build_desktop._maybe_build_installer(root)
        assert installer is None
        assert error is not None and "status 1" in error

    def test_successful_iscc_returns_the_installer_path(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path
        (root / "packaging").mkdir()
        iss = root / "packaging" / "noveldl_installer.iss"
        iss.write_text("; stub", encoding="utf-8")
        out = root / "dist" / "NovelDownloader-setup-9.9.9-x64.exe"
        out.parent.mkdir()
        out.write_bytes(b"installer")
        monkeypatch.setattr(build_desktop, "_find_iscc", lambda: "ISCC.exe")
        monkeypatch.setattr(build_desktop, "_app_version", lambda root: "9.9.9")
        fake_subprocess = types.SimpleNamespace(
            run=lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0)
        )
        monkeypatch.setattr(build_desktop, "subprocess", fake_subprocess)
        installer, error = build_desktop._maybe_build_installer(root)
        assert error is None
        assert installer == out

    def test_missing_output_file_reports_an_error(
        self, build_desktop: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path
        (root / "packaging").mkdir()
        iss = root / "packaging" / "noveldl_installer.iss"
        iss.write_text("; stub", encoding="utf-8")
        monkeypatch.setattr(build_desktop, "_find_iscc", lambda: "ISCC.exe")
        monkeypatch.setattr(build_desktop, "_app_version", lambda root: "9.9.9")
        fake_subprocess = types.SimpleNamespace(
            run=lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0)
        )
        monkeypatch.setattr(build_desktop, "subprocess", fake_subprocess)
        installer, error = build_desktop._maybe_build_installer(root)
        assert installer is None
        assert error is not None and "missing" in error


class TestVerifyScriptExists:
    """packaging/verify.ps1 backs the release SHA256SUMS workflow."""

    def test_verify_ps1_ships_and_parses_sums(self) -> None:
        path = PACKAGING / "verify.ps1"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "SHA256SUMS.txt" in text
        assert "Get-FileHash" in text
        # Zip integrity: every entry is fully read, not just the CRC header.
        assert "ZipFile" in text


def test_verify_archive_converts_zlib_error(monkeypatch, tmp_path):
    """A zlib.error escaping testzip() must become RuntimeError (orphan cleanup)."""
    import zipfile
    import zlib

    import scripts.build_release as br

    archive = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("x.txt", "data")

    class BoomZip(zipfile.ZipFile):
        def testzip(self):
            raise zlib.error("bad checksum stream")

    def fake_zipfile(*args, **kwargs):
        return BoomZip(*args, **kwargs)

    # br.zipfile IS the stdlib module; one patch is enough and monkeypatch
    # restores it automatically.
    monkeypatch.setattr(br.zipfile, "ZipFile", fake_zipfile)
    try:
        br._verify_archive(archive, "bundle")
    except RuntimeError as exc:
        assert "not a readable zip" in str(exc)
    else:
        raise AssertionError("zlib.error must surface as RuntimeError")
