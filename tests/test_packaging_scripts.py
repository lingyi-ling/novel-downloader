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
import ast
import importlib.util
import io
import json
import re
import subprocess
import sys
import tomllib
import types
import urllib.error
import urllib.request
import zipfile
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
SCRIPTS = ROOT / "scripts"
SRC_PACKAGE = ROOT / "src" / "novel_downloader"
ANDROID_GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
WORKFLOWS = (CI_YML, BUILD_YML, RELEASE_YML)
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"
PYPROJECT_TOML = ROOT / "pyproject.toml"
INSTALL_PS1 = ROOT / "bin" / "install.ps1"
ISS = PACKAGING / "noveldl_installer.iss"

_SPEC_COMMON = PACKAGING / "_spec_common.py"
_CLI_SPEC = PACKAGING / "novel_cli.spec"
_API_SPEC = PACKAGING / "novel_api.spec"
_DESKTOP_SPEC = PACKAGING / "novel_desktop.spec"
_RELEASE_SCRIPT = SCRIPTS / "release.py"


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


@pytest.fixture(scope="module")
def release_script() -> Any:
    return _load_script(_RELEASE_SCRIPT, "audit_packaging_release")


@pytest.fixture(scope="module")
def check_versions() -> Any:
    return _load_script(SCRIPTS / "check_versions.py", "audit_check_versions")


@pytest.fixture(scope="module")
def verify_frozen() -> Any:
    return _load_script(SCRIPTS / "verify_frozen.py", "audit_verify_frozen")


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


def _resolved(value: str, env: dict[str, Any]) -> str:
    """Resolve a ``${{ env.NAME }}`` reference against the workflow ``env``."""
    match = re.fullmatch(r"\$\{\{\s*env\.(\w+)\s*\}\}", value)
    return str(env[match.group(1)]) if match else value


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
        env = _load_workflow(BUILD_YML)["env"]
        assert _resolved(python["with"]["python-version"], env) == "3.12"

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
# 4. GitHub Actions workflows: commands, versions, permissions
# ---------------------------------------------------------------------------

_SCRIPT_REF_RE = re.compile(r"\b(?:scripts|packaging)/[\w./-]+\.(?:py|ps1|sh|iss)\b")
_PIP_EXTRAS_RE = re.compile(r'pip install\s+-e\s+"\.\[([^\]]+)\]"')
_NODE_COMMAND_RE = re.compile(r"\b(?:npm|npx|yarn|pnpm|node)\b")


def _load_workflow(path: Path) -> dict[str, Any]:
    """Parse a workflow file (``yaml`` is a hard test dependency in CI)."""
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    assert isinstance(document.get("jobs"), dict), f"{path.name} has no jobs"
    return document


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    """The ``on:`` block; PyYAML parses the bare key as boolean ``True``."""
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), f"unexpected triggers: {triggers!r}"
    return triggers


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps") or [] if isinstance(step, dict)]


def _run_commands(document: dict[str, Any]) -> list[str]:
    """Every ``run:`` script in the workflow, job by job."""
    return [
        str(step["run"])
        for job in document["jobs"].values()
        for step in _steps(job)
        if "run" in step
    ]


def _step_using(job: dict[str, Any], action: str) -> dict[str, Any]:
    for step in _steps(job):
        if action in str(step.get("uses", "")):
            return step
    raise AssertionError(f"no step uses {action}")


def _optional_dependency_names() -> set[str]:
    project = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))["project"]
    return set(project["optional-dependencies"])


class TestWorkflowValidity:
    """Every workflow parses, has jobs, and pins tool versions consistently."""

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_workflow_is_valid_yaml(self, path: Path) -> None:
        document = _load_workflow(path)
        assert document["jobs"]
        assert _triggers(document)

    def test_ci_python_versions_match_pyproject(self) -> None:
        project = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))["project"]
        supported = sorted(
            classifier.rsplit(" ", 1)[-1]
            for classifier in project["classifiers"]
            if classifier.startswith("Programming Language :: Python :: 3.")
        )
        matrix = _load_workflow(CI_YML)["jobs"]["test"]["strategy"]["matrix"]
        assert sorted(str(version) for version in matrix["python-version"]) == supported
        assert project["requires-python"] == f">={supported[0]}"

    def test_ci_tool_versions_match_pre_commit(self) -> None:
        yaml = pytest.importorskip("yaml")
        pre_commit = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
        revs = {
            repository["repo"]: repository["rev"].removeprefix("v")
            for repository in pre_commit["repos"]
        }
        env = _load_workflow(CI_YML)["env"]
        assert env["PYTHON_VERSION"] == "3.12"
        assert (
            env["RUFF_VERSION"] == revs["https://github.com/astral-sh/ruff-pre-commit"]
        )
        assert env["MYPY_VERSION"] == revs["https://github.com/pre-commit/mirrors-mypy"]

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_jobs_declare_timeouts(self, path: Path) -> None:
        # A hung pip/Gradle/network step otherwise burns the 6 h job limit.
        for name, job in _load_workflow(path)["jobs"].items():
            timeout = job.get("timeout-minutes")
            assert isinstance(timeout, int) and timeout > 0, (
                f"{path.name}:{name} has no positive timeout-minutes"
            )

    def test_workflow_permissions_are_minimal(self) -> None:
        ci = _load_workflow(CI_YML)
        build = _load_workflow(BUILD_YML)
        release = _load_workflow(RELEASE_YML)
        assert ci["permissions"] == {"contents": "read"}
        assert build["permissions"] == {"contents": "read"}
        # Publishing needs exactly this; anything broader is unnecessary.
        assert release["permissions"] == {"contents": "write"}

    def test_release_is_not_tag_triggered(self) -> None:
        # A tag-triggered publish races the local scripts/release.py run and
        # overwrites the assets it just uploaded (v1.4.0 regression).
        assert set(_triggers(_load_workflow(RELEASE_YML))) == {"workflow_dispatch"}
        build_triggers = _triggers(_load_workflow(BUILD_YML))
        assert build_triggers["push"]["tags"] == ["v*"]


class TestWorkflowCommands:
    """The commands the workflows run must exist and match this repository."""

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_referenced_scripts_exist(self, path: Path) -> None:
        for command in _run_commands(_load_workflow(path)):
            for match in _SCRIPT_REF_RE.finditer(command):
                referenced = ROOT / match.group(0)
                assert referenced.exists(), (
                    f"{path.name} runs a deleted file: {match.group(0)}"
                )

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_no_node_toolchain(self, path: Path) -> None:
        # The web frontend was removed; no workflow may need Node again.
        document = _load_workflow(path)
        for job in document["jobs"].values():
            for step in _steps(job):
                assert "setup-node" not in str(step.get("uses", ""))
        for command in _run_commands(document):
            assert not _NODE_COMMAND_RE.search(command), (
                f"{path.name} still invokes a Node tool: {command!r}"
            )

    @pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
    def test_pip_extras_are_declared(self, path: Path) -> None:
        declared = _optional_dependency_names()
        for command in _run_commands(_load_workflow(path)):
            for group in _PIP_EXTRAS_RE.findall(command):
                for extra in group.split(","):
                    assert extra.strip() in declared, (
                        f"{path.name} installs undeclared extra {extra!r}"
                    )

    def test_bundle_jobs_install_runtime_extras(self) -> None:
        # `music` ships the music engine; `image-utils` is imported by the
        # image-mode site parsers and the yuewen font decoder. A bundle cannot
        # pip-install either at runtime, so both jobs must build with them.
        for path, jobs in (
            (BUILD_YML, ("python-bundles", "desktop-bundle")),
            (RELEASE_YML, ("publish",)),
        ):
            document = _load_workflow(path)
            for job_name in jobs:
                commands = "\n".join(
                    str(step.get("run", ""))
                    for step in _steps(document["jobs"][job_name])
                )
                expected = {"music", "image-utils"}
                if job_name in {"python-bundles", "publish"}:
                    # novel-api imports fastapi/uvicorn at module load.
                    expected.add("api")
                assert expected <= {
                    extra.strip()
                    for group in _PIP_EXTRAS_RE.findall(commands)
                    for extra in group.split(",")
                }, f"{path.name}:{job_name} is missing {sorted(expected)}"
                # Every bundle job drives PyInstaller directly or through
                # build_release.py; pip must install it explicitly.
                assert "pyinstaller" in commands.lower()

    def test_ci_test_job_installs_yaml_and_runs_pytest(self) -> None:
        job = _load_workflow(CI_YML)["jobs"]["test"]
        commands = "\n".join(str(step.get("run", "")) for step in _steps(job))
        assert "pyyaml" in commands
        assert "pytest -q" in commands

    def test_ci_lint_paths_exist_and_are_used(self) -> None:
        env = _load_workflow(CI_YML)["env"]
        for relative in env["LINT_PATHS"].split():
            assert (ROOT / relative).exists(), f"LINT_PATHS entry {relative} is gone"
        commands = "\n".join(
            str(step.get("run", ""))
            for step in _steps(_load_workflow(CI_YML)["jobs"]["lint"])
        )
        assert "ruff check $LINT_PATHS" in commands
        assert "ruff format --check $LINT_PATHS" in commands

    def test_ci_typecheck_command_matches_local_gate(self) -> None:
        commands = "\n".join(
            str(step.get("run", ""))
            for step in _steps(_load_workflow(CI_YML)["jobs"]["typecheck"])
        )
        assert "mypy src" in commands
        for script in (
            "scripts/build_cli.py",
            "scripts/build_desktop.py",
            "scripts/build_release.py",
            "scripts/check_versions.py",
            "scripts/verify_frozen.py",
            "scripts/release.py",
        ):
            assert script in commands

    def test_translations_job_regenerates_catalogs(self) -> None:
        job = _load_workflow(CI_YML)["jobs"]["translations"]
        commands = "\n".join(str(step.get("run", "")) for step in _steps(job))
        assert "scripts/update_pot.py" in commands
        assert "scripts/update_po.py" in commands
        assert "git diff --exit-code -- src/novel_downloader/locales" in commands
        # The freshness check compares Babel's exact output, so the version is
        # pinned instead of floating on >=.
        assert re.search(r"babel==\d", commands), "babel must be pinned"


class TestCiAndroidJob:
    """ci.yml's Android job must match build.yml and the Gradle files."""

    def test_android_job_mirrors_build_workflow(self) -> None:
        gradle = ANDROID_GRADLE.read_text(encoding="utf-8")
        ci_job = _load_workflow(CI_YML)["jobs"]["android"]
        build_job = _load_workflow(BUILD_YML)["jobs"]["android-apk"]

        # Chaquopy's buildPython (android/app/build.gradle.kts) is 3.12.
        assert 'version = "3.12"' in gradle
        for job, path in ((ci_job, CI_YML), (build_job, BUILD_YML)):
            python = _step_using(job, "setup-python")
            env = _load_workflow(path)["env"]
            assert _resolved(python["with"]["python-version"], env) == "3.12"

        # Gradle compiles with Java 17 and compileSdk 35 (AGP 8.6.1).
        assert "VERSION_17" in gradle and "compileSdk = 35" in gradle
        for job in (ci_job, build_job):
            java = _step_using(job, "setup-java")
            assert java["with"]["java-version"] == "17"

        packages = _step_using(ci_job, "setup-android")["with"]["packages"]
        assert packages == _step_using(build_job, "setup-android")["with"]["packages"]
        assert "platforms;android-35" in packages
        assert "build-tools;35.0.0" in packages
        assert "build-tools;34.0.0" in packages

        commands = "\n".join(str(step.get("run", "")) for step in _steps(ci_job))
        assert "chmod +x android/gradlew" in commands
        assert "./gradlew testDebugUnitTest --no-daemon" in commands

    def test_build_artifacts_fail_when_missing(self) -> None:
        document = _load_workflow(BUILD_YML)
        for job_name in ("python-bundles", "desktop-bundle"):
            uploads = [
                step
                for step in _steps(document["jobs"][job_name])
                if "upload-artifact" in str(step.get("uses", ""))
            ]
            assert uploads
            for step in uploads:
                assert step["with"]["if-no-files-found"] == "error"
                assert step["with"]["path"].startswith("dist/")
        apk = [
            step
            for step in _steps(document["jobs"]["android-apk"])
            if "upload-artifact" in str(step.get("uses", ""))
        ]
        assert apk and apk[0]["with"]["name"] == "novel-downloader-android-debug"


class TestBuildArtifactPaths:
    """Upload paths must match what the build scripts actually write."""

    def test_python_bundle_paths_match_build_cli(self, build_cli: Any) -> None:
        document = _load_workflow(BUILD_YML)
        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for step in _steps(document["jobs"]["python-bundles"])
            if "upload-artifact" in str(step.get("uses", ""))
        }
        assert uploads == {
            "novel-cli-windows-x64": f"dist/{build_cli.TARGETS['cli'][0]}",
            "novel-api-windows-x64": f"dist/{build_cli.TARGETS['api'][0]}",
        }
        commands = "\n".join(
            str(step.get("run", ""))
            for step in _steps(document["jobs"]["python-bundles"])
        )
        assert "scripts/build_cli.py --target both" in commands

    def test_desktop_bundle_path_matches_build_desktop(
        self, build_desktop: Any
    ) -> None:
        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for step in _steps(_load_workflow(BUILD_YML)["jobs"]["desktop-bundle"])
            if "upload-artifact" in str(step.get("uses", ""))
        }
        assert uploads == {
            "novel-desktop-windows-x64": f"dist/{build_desktop.APP_NAME}"
        }


class TestReleaseWorkflow:
    """The publish job needs full history, a token, and nothing else."""

    def test_publish_job_is_dispatch_ready(self) -> None:
        job = _load_workflow(RELEASE_YML)["jobs"]["publish"]
        checkout = _step_using(job, "actions/checkout")
        # Full history so scripts/release.py can see the tag it is releasing.
        assert checkout["with"]["fetch-depth"] == 0
        # The token is handed to release.py via the environment only.
        assert checkout["with"]["persist-credentials"] is False

        publish = next(
            step
            for step in _steps(job)
            if str(step.get("run", "")).strip() == "python scripts/release.py"
        )
        assert publish["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}

    def test_publish_job_has_no_android_or_secret_dependencies(self) -> None:
        # The runner has no Android SDK, JDK or signing keystore; release.py
        # must be able to publish desktop/CLI assets in that environment.
        job = _load_workflow(RELEASE_YML)["jobs"]["publish"]
        for step in _steps(job):
            assert "android" not in str(step.get("uses", "")).lower()
            assert "setup-java" not in str(step.get("uses", ""))
        commands = "\n".join(str(step.get("run", "")) for step in _steps(job))
        assert "assembleRelease" not in commands
        assert "gradlew" not in commands
        assert "secrets." not in commands.replace("${{ secrets.GITHUB_TOKEN }}", "")


# ---------------------------------------------------------------------------
# 5. build_release.py: naming, verification, summary, manifest
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
# 6. Exit-code semantics of the build wrappers
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


# ---------------------------------------------------------------------------
# 7. Windows version resource (FileVersion / ProductVersion)
# ---------------------------------------------------------------------------

_VER = "9.9.9"
_TAG = f"v{_VER}"
_SLUG = "tester/example"


class TestWindowsVersionResource:
    """The version resource embedded in every Windows executable."""

    def test_version_numbers_pads_and_drops_suffixes(self, spec_common: Any) -> None:
        assert spec_common.version_numbers("1.28.1") == (1, 28, 1, 0)
        assert spec_common.version_numbers("2.0") == (2, 0, 0, 0)
        assert spec_common.version_numbers("1.2.3.4") == (1, 2, 3, 4)
        # FixedFileInfo has four numeric fields: pre-release/local suffixes
        # cannot live there (they still appear in the string fields).
        assert spec_common.version_numbers("1.2.3-rc1") == (1, 2, 3, 0)
        assert spec_common.version_numbers("1.2.3+build.7") == (1, 2, 3, 0)
        assert spec_common.version_numbers("10.0.19045.1") == (10, 0, 19045, 1)

    def test_reads_the_packaged_source_version(self, spec_common: Any) -> None:
        init_py = (SRC_PACKAGE / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
        assert match is not None
        assert spec_common.read_package_version(ROOT / "src") == match.group(1)

    def test_missing_package_version_is_an_error(
        self, spec_common: Any, tmp_path: Path
    ) -> None:
        with pytest.raises(RuntimeError, match="version file not found"):
            spec_common.read_package_version(tmp_path)

    def test_resource_carries_file_and_product_version(self, spec_common: Any) -> None:
        text = spec_common.windows_version_resource(
            "1.28.1", app_name="NovelDownloader"
        )
        ast.parse(text)  # PyInstaller eval()s this: it must be valid Python
        assert "filevers=(1, 28, 1, 0)" in text
        assert "prodvers=(1, 28, 1, 0)" in text
        assert "StringStruct('FileVersion', '1.28.1')" in text
        assert "StringStruct('ProductVersion', '1.28.1')" in text
        assert "StringStruct('InternalName', 'NovelDownloader')" in text
        assert "StringStruct('OriginalFilename', 'NovelDownloader.exe')" in text
        assert "VarStruct('Translation', [1033, 1200])" in text

    def test_description_defaults_to_the_app_name(self, spec_common: Any) -> None:
        default = spec_common.windows_version_resource("1.0.0", app_name="novel-api")
        assert "StringStruct('FileDescription', 'novel-api application')" in default
        custom = spec_common.windows_version_resource(
            "1.0.0", app_name="novel-api", description="novel-api HTTP service"
        )
        assert "StringStruct('FileDescription', 'novel-api HTTP service')" in custom

    def test_written_file_uses_the_repository_version(
        self, spec_common: Any, tmp_path: Path
    ) -> None:
        version = spec_common.read_package_version(ROOT / "src")
        path = spec_common.write_version_resource(
            tmp_path / "work", ROOT / "src", app_name="novel-cli"
        )
        assert path == tmp_path / "work" / "version_info" / "novel-cli.txt"
        text = path.read_text(encoding="utf-8")
        ast.parse(text)
        numbers = spec_common.version_numbers(version)
        assert f"filevers={numbers}" in text
        assert f"prodvers={numbers}" in text
        assert f"StringStruct('FileVersion', '{version}')" in text
        assert f"StringStruct('ProductVersion', '{version}')" in text

    def test_pyinstaller_can_evaluate_the_resource(
        self, spec_common: Any, tmp_path: Path
    ) -> None:
        versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
        version = spec_common.read_package_version(ROOT / "src")
        path = spec_common.write_version_resource(
            tmp_path, ROOT / "src", app_name="NovelDownloader"
        )
        info = versioninfo.load_version_info_from_text_file(str(path))
        major, minor, build, revision = spec_common.version_numbers(version)
        assert info.ffi.fileVersionMS == (major << 16) | minor
        assert info.ffi.fileVersionLS == (build << 16) | revision
        fields = {item.name: item.val for item in info.kids[0].kids[0].kids}
        assert fields["FileVersion"] == version
        assert fields["ProductVersion"] == version

    @pytest.mark.parametrize(
        ("spec_path", "app_name"),
        [
            (_CLI_SPEC, "novel-cli"),
            (_API_SPEC, "novel-api"),
            (_DESKTOP_SPEC, "NovelDownloader"),
        ],
    )
    def test_specs_embed_the_resource(self, spec_path: Path, app_name: str) -> None:
        text = spec_path.read_text(encoding="utf-8")
        assert f'APP_NAME = "{app_name}"' in text
        # Generated into PyInstaller's work directory, never the source tree.
        assert "write_version_resource(" in text
        assert "Path(workpath)" in text
        assert "app_name=APP_NAME" in text
        # And actually handed to the EXE.
        assert "version=str(VERSION_FILE)" in text


# ---------------------------------------------------------------------------
# 8. Installer upgrade contract (AppId / mutex / stale payload cleanup)
# ---------------------------------------------------------------------------


class TestInstallerUpgradeContract:
    def test_appid_is_a_stable_guid(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        match = re.search(r"^AppId=(\{\{[0-9A-Fa-f-]{36}\})", text, re.MULTILINE)
        assert match is not None, "AppId must be a fixed GUID (Inno {{escaping}})"
        # Inno escapes a literal opening brace by doubling it.
        assert match.group(1).count("{") == 2

    def test_close_applications_and_mutex_match_the_desktop_app(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        assert "CloseApplications=yes" in text
        mutex = re.search(r"^AppMutex=(\S+)", text, re.MULTILINE)
        assert mutex is not None

        webview = (SRC_PACKAGE / "apps" / "desktop" / "webview_app.py").read_text(
            encoding="utf-8"
        )
        name = re.search(r'_UPDATE_MUTEX_NAME\s*=\s*"([^"]+)"', webview)
        assert name is not None, "the desktop app no longer creates the mutex"
        assert mutex.group(1) == name.group(1), (
            "AppMutex and _UPDATE_MUTEX_NAME must stay identical or Setup cannot "
            "close a running instance"
        )

    def test_install_delete_cleans_only_the_payload_tree(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        assert "[InstallDelete]" in text
        section_start = re.search(r"^\[InstallDelete\]", text, re.MULTILINE)
        files_start = re.search(r"^\[Files\]", text, re.MULTILINE)
        assert section_start is not None and files_start is not None
        # InstallDelete must run before [Files] copies the new payload.
        assert section_start.start() < files_start.start()

        section = text[section_start.end() : files_start.start()]
        assert r'Name: "{app}\_internal"' in section
        assert "Type: filesandordirs" in section
        assert section.count("Type:") == 1
        # Never the whole install dir, and never user data.
        assert 'Name: "{app}"' not in section
        assert r"localappdata}\novel_downloader" not in section
        assert "[UninstallDelete]" not in text


# ---------------------------------------------------------------------------
# 9. release.py: tag push fallback, asset resume, latest.json validation
# ---------------------------------------------------------------------------


def _asset_spec(
    module: Any, directory: Path, name: str, data: bytes = b"payload"
) -> Any:
    """Write *data* to ``directory/name`` and wrap it as an AssetSpec."""
    path = directory / name
    path.write_bytes(data)
    return module.AssetSpec(path, name, sha256(data).hexdigest())


class TestTagPushFallback:
    """github.com may be unreachable while api.github.com still answers."""

    def test_push_failure_uses_rest_with_the_configured_api_base(
        self, release_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []
        created: dict[str, Any] = {}

        def fake_run(command: Any, **kwargs: Any) -> Any:
            recorded = list(command)
            calls.append((recorded, kwargs))
            if recorded[:2] == ["git", "push"]:
                raise release_script.ReleaseError("could not resolve host: github.com")
            if recorded[:2] == ["git", "rev-parse"]:
                return types.SimpleNamespace(
                    returncode=0, stdout="deadbeef\n", stderr=""
                )
            raise AssertionError(f"unexpected command: {recorded}")

        class StubClient:
            def __init__(
                self,
                slug: str,
                token: str,
                api_base: str = release_script.GITHUB_API,
            ) -> None:
                created.update(slug=slug, token=token, api_base=api_base)

            def ensure_tag_ref(self, tag: str, sha: str) -> None:
                created.update(tag=tag, sha=sha)

        monkeypatch.setattr(release_script, "_run", fake_run)
        monkeypatch.setattr(release_script, "GitHubClient", StubClient)

        release_script.push_tag(
            tmp_path,
            _TAG,
            "tok",
            _SLUG,
            api_base="https://ghe.example/api/v3",
        )

        assert created == {
            "slug": _SLUG,
            "token": "tok",
            "api_base": "https://ghe.example/api/v3",
            "tag": _TAG,
            "sha": "deadbeef",
        }
        push_kwargs = next(
            kwargs for command, kwargs in calls if command[:2] == ["git", "push"]
        )
        assert push_kwargs["timeout"] == release_script.PUSH_TIMEOUT

    def test_both_paths_failing_reports_both(
        self, release_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: Any, **kwargs: Any) -> Any:
            recorded = list(command)
            if recorded[:2] == ["git", "push"]:
                raise release_script.ReleaseError("host down")
            return types.SimpleNamespace(returncode=0, stdout="abc\n", stderr="")

        class StubClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def ensure_tag_ref(self, tag: str, sha: str) -> None:
                raise release_script.ReleaseError("HTTP 500")

        monkeypatch.setattr(release_script, "_run", fake_run)
        monkeypatch.setattr(release_script, "GitHubClient", StubClient)

        with pytest.raises(release_script.ReleaseError) as info:
            release_script.push_tag(tmp_path, _TAG, "tok", _SLUG)
        message = str(info.value)
        assert "host down" in message
        assert "HTTP 500" in message

    def test_without_a_token_the_push_error_is_raised(
        self, release_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        constructed: list[bool] = []

        def fake_run(command: Any, **kwargs: Any) -> Any:
            raise release_script.ReleaseError("authentication failed")

        class StubClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                constructed.append(True)

        monkeypatch.setattr(release_script, "_run", fake_run)
        monkeypatch.setattr(release_script, "GitHubClient", StubClient)

        with pytest.raises(release_script.ReleaseError, match="authentication"):
            release_script.push_tag(tmp_path, _TAG, None, _SLUG)
        assert constructed == []

    def test_remote_query_timeout_is_not_fatal(
        self,
        release_script: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seen: list[dict[str, Any]] = []

        def fake_run(command: Any, **kwargs: Any) -> Any:
            seen.append(kwargs)
            raise release_script.ReleaseError("command timed out after 60s")

        monkeypatch.setattr(release_script, "_run", fake_run)

        assert release_script.remote_tag_exists(tmp_path, _TAG) is False
        assert seen[0]["timeout"] == release_script.GIT_REMOTE_TIMEOUT
        assert "assuming it is not pushed" in capsys.readouterr().err


class _SyncClient:
    """Records the two calls sync_assets makes."""

    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.uploaded: list[str] = []

    def delete_asset(self, asset_id: int) -> None:
        self.deleted.append(asset_id)

    def upload_asset(self, upload_url: str, asset: Any) -> None:
        self.uploaded.append(asset.name)


class TestAssetSyncResume:
    """Re-running a half-finished release must not redo finished uploads."""

    @staticmethod
    def _release(name: str, digest: str | None) -> dict[str, Any]:
        return {
            "upload_url": "https://uploads.example/assets{?name,label}",
            "assets": [{"name": name, "id": 41, "digest": digest}],
        }

    def test_matching_digest_skips_the_upload(
        self,
        release_script: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        asset = _asset_spec(release_script, tmp_path, "novel-cli-x.zip")
        client = _SyncClient()
        release_script.sync_assets(
            client,
            self._release(asset.name, f"sha256:{asset.sha256}"),
            [asset],
        )
        assert client.uploaded == []
        assert client.deleted == []
        assert "up to date, skipping" in capsys.readouterr().out

    def test_unknown_digest_replaces_the_asset(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        asset = _asset_spec(release_script, tmp_path, "novel-cli-x.zip")
        client = _SyncClient()
        release_script.sync_assets(client, self._release(asset.name, None), [asset])
        # Without a remote digest the bytes cannot be compared, so the only
        # safe action is a replace (delete then upload).
        assert client.deleted == [41]
        assert client.uploaded == [asset.name]

    def test_a_remote_asset_without_an_id_is_an_error(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        asset = _asset_spec(release_script, tmp_path, "novel-cli-x.zip")
        release = {
            "upload_url": "https://uploads.example/assets{?name,label}",
            "assets": [{"name": asset.name, "digest": "sha256:" + "0" * 64}],
        }
        with pytest.raises(release_script.ReleaseError, match="no usable id"):
            release_script.sync_assets(_SyncClient(), release, [asset])


class TestLatestManifestValidation:
    """latest.json must describe exactly the files being uploaded."""

    def _specs(self, release_script: Any, tmp_path: Path) -> list[Any]:
        return [
            _asset_spec(
                release_script,
                tmp_path,
                f"novel-cli-{_VER}-windows-x64.zip",
                b"cli",
            ),
            _asset_spec(
                release_script,
                tmp_path,
                f"NovelDownloader-{_VER}-windows-x64.zip",
                b"portable",
            ),
            _asset_spec(
                release_script,
                tmp_path,
                f"NovelDownloader-setup-{_VER}-x64.exe",
                b"setup",
            ),
        ]

    def _document(self, release_script: Any, tmp_path: Path) -> tuple[list[Any], Any]:
        specs = self._specs(release_script, tmp_path)
        document = release_script.build_manifest_document(specs, _VER, _TAG, _SLUG)
        return specs, document

    def test_document_matches_the_local_assets(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        specs, document = self._document(release_script, tmp_path)
        assert set(document["platforms"]) == {
            "windows-x64-cli",
            "windows-x64-portable",
            "windows-x64-installer",
        }
        assert document["version"] == _VER
        assert document["tag"] == _TAG
        assert document["repo"] == _SLUG
        for entry in document["platforms"].values():
            asset = next(item for item in specs if item.name == entry["name"])
            assert entry["sha256"] == asset.sha256
            assert entry["size"] == asset.path.stat().st_size
            assert entry["url"].startswith(
                f"https://github.com/{_SLUG}/releases/download/{_TAG}/"
            )

    def test_tampered_digest_is_rejected(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        specs, document = self._document(release_script, tmp_path)
        key = sorted(document["platforms"])[0]
        document["platforms"][key]["sha256"] = "0" * 64
        with pytest.raises(release_script.ReleaseError, match="sha256"):
            release_script.validate_latest_manifest(
                document, specs, version=_VER, tag=_TAG, slug=_SLUG
            )

    def test_tampered_size_is_rejected(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        specs, document = self._document(release_script, tmp_path)
        key = sorted(document["platforms"])[0]
        document["platforms"][key]["size"] = 1
        with pytest.raises(release_script.ReleaseError, match="size"):
            release_script.validate_latest_manifest(
                document, specs, version=_VER, tag=_TAG, slug=_SLUG
            )

    def test_foreign_url_is_rejected(self, release_script: Any, tmp_path: Path) -> None:
        specs, document = self._document(release_script, tmp_path)
        key = sorted(document["platforms"])[0]
        document["platforms"][key]["url"] = "https://evil.example/file.zip"
        with pytest.raises(release_script.ReleaseError, match="expected"):
            release_script.validate_latest_manifest(
                document, specs, version=_VER, tag=_TAG, slug=_SLUG
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("version", "0.0.1"), ("tag", "v0.0.1"), ("repo", "other/repo")],
    )
    def test_identity_fields_are_enforced(
        self, release_script: Any, tmp_path: Path, field: str, value: str
    ) -> None:
        specs, document = self._document(release_script, tmp_path)
        document[field] = value
        with pytest.raises(release_script.ReleaseError, match=field):
            release_script.validate_latest_manifest(
                document, specs, version=_VER, tag=_TAG, slug=_SLUG
            )

    def test_asset_missing_from_the_manifest_is_rejected(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        specs = self._specs(release_script, tmp_path)
        extra = _asset_spec(
            release_script,
            tmp_path,
            f"mystery-{_VER}-windows-x64.zip",
            b"unclassified",
        )
        with pytest.raises(release_script.ReleaseError, match="would not advertise"):
            release_script.build_manifest_document([*specs, extra], _VER, _TAG, _SLUG)

    def test_written_latest_json_is_consistent(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        specs = self._specs(release_script, tmp_path)
        release_dir = tmp_path / "out"
        release_dir.mkdir()
        asset = release_script.build_latest_manifest(
            specs, release_dir, _VER, _TAG, _SLUG
        )
        assert asset.name == "latest.json"
        payload = (release_dir / "latest.json").read_bytes()
        assert asset.sha256 == sha256(payload).hexdigest()
        written = json.loads(payload.decode("utf-8"))
        release_script.validate_latest_manifest(
            written, specs, version=_VER, tag=_TAG, slug=_SLUG
        )

    def test_manifest_digest_is_required_at_verification(
        self, release_script: Any, tmp_path: Path
    ) -> None:
        asset = _asset_spec(release_script, tmp_path, "latest.json", b"{}")

        class _Client:
            def get_release_by_tag(self, tag: str) -> dict[str, Any]:
                return {"assets": [{"name": "latest.json"}]}

        with pytest.raises(
            release_script.ReleaseError, match="no digest for latest.json"
        ):
            release_script.verify_assets(_Client(), _TAG, [asset])


class TestTransientClassification:
    def test_rate_limited_403_is_transient(
        self, release_script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.github.com/x",
            403,
            "Forbidden",
            None,
            io.BytesIO(b'{"message": "API rate limit exceeded for 1.2.3.4"}'),
        )

        def fake_urlopen(*args: Any, **kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = release_script.GitHubClient(_SLUG, "tok")
        with pytest.raises(release_script.TransientAPIError):
            client.get_release_by_tag(_TAG)


class TestDryRunManifestPreview:
    """--dry-run must cover the tag, the assets and latest.json."""

    def _make_repo(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        package = root / "src" / "novel_downloader"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            f'__version__ = "{_VER}"\n', encoding="utf-8"
        )
        (root / "CHANGELOG.md").write_text(
            f"## Unreleased\n\n- later\n\n## v{_VER} (2026-01-01)\n\n- shipped\n",
            encoding="utf-8",
        )
        release_dir = root / "dist" / "release" / f"novel-downloader-{_VER}-windows-x64"
        release_dir.mkdir(parents=True)
        for name in (
            f"novel-cli-{_VER}-windows-x64.zip",
            f"novel-api-{_VER}-windows-x64.zip",
            f"NovelDownloader-{_VER}-windows-x64.zip",
        ):
            (release_dir / name).write_bytes(f"data-{name}".encode())
        return root, release_dir

    def test_plan_lists_tag_assets_and_manifest_without_side_effects(
        self,
        release_script: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, release_dir = self._make_repo(tmp_path)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"dry run must not execute anything: {args!r}")

        monkeypatch.setattr(release_script, "REPO_ROOT", root)
        monkeypatch.setattr(
            release_script,
            "subprocess",
            types.SimpleNamespace(run=forbidden),
        )
        monkeypatch.setattr(urllib.request, "urlopen", forbidden)

        assert release_script.main(["--dry-run"]) == 0

        out = capsys.readouterr().out
        assert f"Tag         : {_TAG}" in out
        assert f"git tag {_TAG}" in out
        assert f"git push origin {_TAG}" in out
        assert f"novel-cli-{_VER}-windows-x64.zip" in out
        assert "latest.json  (3 platform(s)" in out
        assert "windows-x64-cli" in out
        assert "windows-x64-api" in out
        assert "windows-x64-portable" in out
        assert "unsigned (pass --sign-key to sign)" in out
        assert not (release_dir / "latest.json").exists()


class TestEnsureTagRef:
    """The REST tag fallback must not point a release at another commit."""

    @staticmethod
    def _client(release_script: Any) -> Any:
        return release_script.GitHubClient(_SLUG, "tok", api_base="https://api.example")

    @staticmethod
    def _ref_payload(sha: str) -> dict[str, Any]:
        return {"ref": f"refs/tags/{_TAG}", "object": {"type": "commit", "sha": sha}}

    def test_identical_existing_ref_is_accepted(
        self, release_script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(release_script)
        replies = iter(
            [
                release_script.Reply(422, {"message": "Reference already exists"}),
                release_script.Reply(200, self._ref_payload("abc123")),
            ]
        )
        monkeypatch.setattr(client, "_request", lambda *a, **k: next(replies))

        client.ensure_tag_ref(_TAG, "abc123")  # must not raise

    def test_existing_ref_at_another_commit_is_rejected(
        self, release_script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(release_script)
        replies = iter(
            [
                release_script.Reply(422, {"message": "Reference already exists"}),
                release_script.Reply(200, self._ref_payload("0ld")),
            ]
        )
        monkeypatch.setattr(client, "_request", lambda *a, **k: next(replies))

        with pytest.raises(
            release_script.ReleaseError, match="already exists on the remote at 0ld"
        ):
            client.ensure_tag_ref(_TAG, "new")


# ---------------------------------------------------------------------------
# 10. Version consistency gate (scripts/check_versions.py)
# ---------------------------------------------------------------------------


def _make_version_repo(
    root: Path, *, package: str = "1.2.3", android: str = "1.2.3"
) -> Path:
    """A minimal tree exposing only the two hand-maintained version sources."""
    package_dir = root / "src" / "novel_downloader"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        f'__version__ = "{package}"\n', encoding="utf-8"
    )
    gradle_dir = root / "android" / "app"
    gradle_dir.mkdir(parents=True)
    (gradle_dir / "build.gradle.kts").write_text(
        f'versionCode = 7\nversionName = "{android}"\n', encoding="utf-8"
    )
    return root


class TestVersionConsistencyGate:
    """Every hand-maintained copy of the version must agree."""

    def test_repository_versions_agree(self, check_versions: Any) -> None:
        checks = check_versions.run_checks(ROOT)
        failed = [check for check in checks if not check.ok]
        assert not failed, [(check.name, check.detail) for check in failed]
        assert {check.name for check in checks} == {
            "package/Android versionName",
            "pyproject version source",
            "latest.json version source",
            "local latest.json",
        }

    def test_main_passes_on_the_repository(
        self, check_versions: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert check_versions.main(["--root", str(ROOT)]) == 0
        out = capsys.readouterr().out
        assert "All version sources agree." in out

    def test_android_drift_is_detected(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        root = _make_version_repo(tmp_path, package="1.2.3", android="1.2.4")
        check = check_versions.check_package_and_android(root)
        assert not check.ok
        assert "1.2.4" in check.detail and "1.2.3" in check.detail

    @pytest.mark.parametrize("version_name", ["1.2.3", "2.0.0"])
    def test_matching_android_version_passes(
        self, check_versions: Any, tmp_path: Path, version_name: str
    ) -> None:
        root = _make_version_repo(tmp_path, package=version_name, android=version_name)
        assert check_versions.check_package_and_android(root).ok

    def test_missing_android_file_is_a_failure(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        root = _make_version_repo(tmp_path)
        (root / "android" / "app" / "build.gradle.kts").unlink()
        assert not check_versions.check_package_and_android(root).ok

    def test_static_pyproject_version_is_rejected(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8"
        )
        check = check_versions.check_pyproject_dynamic(tmp_path)
        assert not check.ok
        assert "dynamic" in check.detail

    def test_dynamic_version_must_point_at_the_package(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "x"\n'
            'dynamic = ["version"]\n'
            "[tool.setuptools.dynamic]\n"
            'version = {attr = "other.__version__"}\n',
            encoding="utf-8",
        )
        check = check_versions.check_pyproject_dynamic(tmp_path)
        assert not check.ok
        assert check_versions.EXPECTED_VERSION_ATTR in check.detail

    def test_real_pyproject_is_dynamic(self, check_versions: Any) -> None:
        assert check_versions.check_pyproject_dynamic(ROOT).ok

    def test_release_reads_the_package_version(self, check_versions: Any) -> None:
        check = check_versions.check_release_version_source(ROOT)
        assert check.ok
        assert check_versions.read_package_version(ROOT) in check.detail

    def test_local_manifest_mismatch_is_detected(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        root = _make_version_repo(tmp_path)
        release_dir = root / "dist" / "release" / "novel-downloader-1.2.3-windows-x64"
        release_dir.mkdir(parents=True)
        manifest = release_dir / "latest.json"
        manifest.write_text(
            json.dumps({"version": "1.2.4", "tag": "v1.2.4"}), encoding="utf-8"
        )
        assert not check_versions.check_local_latest_json(root).ok

        manifest.write_text(
            json.dumps({"version": "1.2.3", "tag": "v1.2.3"}), encoding="utf-8"
        )
        assert check_versions.check_local_latest_json(root).ok

    def test_absent_local_manifest_is_not_a_failure(
        self, check_versions: Any, tmp_path: Path
    ) -> None:
        root = _make_version_repo(tmp_path)
        check = check_versions.check_local_latest_json(root)
        assert check.ok
        assert "not built" in check.detail

    def test_ci_runs_the_gate(self) -> None:
        job = _load_workflow(CI_YML)["jobs"]["versions"]
        commands = "\n".join(str(step.get("run", "")) for step in _steps(job))
        assert "scripts/check_versions.py" in commands


# ---------------------------------------------------------------------------
# 11. Frozen bundle smoke test (scripts/verify_frozen.py)
# ---------------------------------------------------------------------------


class TestFrozenVerification:
    """The post-build gate must fail loudly, not silently skip."""

    def test_bundle_map_covers_the_three_launchers(self, verify_frozen: Any) -> None:
        assert set(verify_frozen.BUNDLES) == {"cli", "api", "desktop"}
        assert verify_frozen.BUNDLES["cli"][1] == "novel-cli.exe"
        assert verify_frozen.BUNDLES["api"][1] == "novel-api.exe"
        assert verify_frozen.BUNDLES["desktop"][1] == "NovelDownloader.exe"

    def test_missing_bundles_fail_with_a_clear_message(
        self,
        verify_frozen: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _make_version_repo(tmp_path)
        assert verify_frozen.main(["--root", str(root), "--bundles", "cli,api"]) != 0
        out = capsys.readouterr().out
        assert "missing launcher" in out
        assert "failed verification" in out

    def test_unknown_bundle_is_rejected(self, verify_frozen: Any) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            verify_frozen._parse_bundles("cli,android")

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("cli", ["cli"]),
            (" cli , api ", ["cli", "api"]),
            ("cli,cli,desktop", ["cli", "desktop"]),
        ],
    )
    def test_parse_bundles(
        self, verify_frozen: Any, value: str, expected: list[str]
    ) -> None:
        assert verify_frozen._parse_bundles(value) == expected

    def test_image_stack_matches_spec_common(
        self, verify_frozen: Any, spec_common: Any
    ) -> None:
        assert (
            spec_common.IMAGE_STACK_HIDDEN_IMPORTS == verify_frozen.IMAGE_STACK_MODULES
        )
        assert (
            verify_frozen._expected_image_modules(ROOT)
            == spec_common.IMAGE_STACK_HIDDEN_IMPORTS
        )

    def test_image_stack_is_required_by_the_source(self, verify_frozen: Any) -> None:
        # Keep the list honest: these are the modules the optional image
        # dependencies actually import.
        image_utils = (SRC_PACKAGE / "libs" / "image_utils.py").read_text(
            encoding="utf-8"
        )
        font_utils = (SRC_PACKAGE / "libs" / "font_utils.py").read_text(
            encoding="utf-8"
        )
        assert "import numpy" in image_utils
        assert "from PIL import" in image_utils
        assert "fontTools" in font_utils
        assert set(verify_frozen.IMAGE_STACK_MODULES) >= {"numpy", "fontTools"}

    def test_build_workflow_runs_the_smoke_test(self) -> None:
        document = _load_workflow(BUILD_YML)
        python_job = "\n".join(
            str(step.get("run", ""))
            for step in _steps(document["jobs"]["python-bundles"])
        )
        desktop_job = "\n".join(
            str(step.get("run", ""))
            for step in _steps(document["jobs"]["desktop-bundle"])
        )
        assert "scripts/verify_frozen.py --bundles cli,api" in python_job
        assert "scripts/verify_frozen.py --bundles desktop" in desktop_job


class TestSpecImageStack:
    """The image stack must be collected by every PyInstaller spec."""

    def test_image_modules_are_hidden_imports(self, spec_common: Any) -> None:
        for module in spec_common.IMAGE_STACK_HIDDEN_IMPORTS:
            assert module in spec_common.EXTRA_HIDDEN_IMPORTS

    def test_image_modules_reach_the_specs(self, spec_common: Any) -> None:
        hidden = set(spec_common.common_hidden_imports(ROOT / "src"))
        assert set(spec_common.IMAGE_STACK_HIDDEN_IMPORTS) <= hidden

    def test_image_modules_are_never_excluded(self, spec_common: Any) -> None:
        excluded_roots = {name.split(".")[0] for name in spec_common.EXCLUDES}
        assert not excluded_roots & {"numpy", "PIL", "fontTools"}
