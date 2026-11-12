#!/usr/bin/env python3
"""Chaquopy dependency consistency: build list vs pyproject vs runtime guards.

The Android engine runs the same service layer as the desktop/CLI bundles, so
its pip block must carry the same runtime extras.  A missing package is *not*
loud at build time: ``musicdl.LIGHT_PLATFORMS`` probes with
``importlib.util.find_spec`` and simply does not register the affected
platform, so the user only sees a shorter platform list (or a first-search
error).  These tests make the drift visible before an APK is built:

* every module-level third-party import in ``apps/music`` is declared by
  ``[project.dependencies]`` or the ``music`` extra;
* every declared runtime distribution is either installed by Chaquopy or
  listed in :data:`ANDROID_NOT_INSTALLED` with a reason and a runtime guard;
* packages that have no Android wheel (``curl_cffi``, ``orjson``, ...) are not
  installed by accident;
* Chaquopy's Python version, ``buildPython``, CI's interpreter and the ABIs
  stay aligned;
* version floors copied from pyproject do not exceed the newest wheel in
  Chaquopy's repository (pip would fail to resolve during the APK build).

No Gradle invocation and no network access is involved.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
ANDROID_GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
PYPROJECT_TOML = ROOT / "pyproject.toml"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
MUSIC_TREE = ROOT / "src" / "novel_downloader" / "apps" / "music"

#: Import name -> PyPI distribution name where they differ.
MODULE_TO_DIST = {
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "fake_useragent": "fake-useragent",
    "json_repair": "json-repair",
    "pkg_resources": "setuptools",
    "typing_extensions": "typing-extensions",
    "unidecode": "unidecode",
    "websocket": "websocket-client",
    "yaml": "pyyaml",
}

#: Distributions the music engine needs on every platform.  Nothing else in
#: the extras pulls these in, so leaving one out breaks a set of clients at
#: import time (before any network request).
MUSIC_RUNTIME_DISTS = {
    "mutagen",
    "pathvalidate",
    "pillow",
    "qrcode",
    "websocket-client",
    "m3u8",
    "emoji",
    "bleach",
    "filetype",
    "puremagic",
    "fake-useragent",
    "json-repair",
    "tinytag",
    "tabulate",
    "prettytable",
    "wcwidth",
    "prompt-toolkit",
    "aigpy",
    "ytmusicapi",
    "pywidevine",
    "beautifulsoup4",
    "cryptography",
    "typing-extensions",
    "orjson",
}

#: Runtime distributions deliberately left out of the APK, and why.  Every
#: entry must also be either unused at module level or guarded by
#: ``musicdl.PLATFORM_DEPS`` (asserted below), so a platform never registers
#: and then fails on its first search.
ANDROID_NOT_INSTALLED: dict[str, str] = {
    "pywebview": "desktop GUI; the embedded engine is a stdlib HTTP server",
    "curl-cffi": "compiled backend with no Android wheel; aiohttp is default",
    "httpx": "optional alternate backend; the embedded engine never selects it",
    "orjson": "no Android wheel; QQ is gated by musicdl.PLATFORM_DEPS",
    "fastapi": "the embedded server is stdlib-only; pydantic-core has no wheel",
    "uvicorn": "only the FastAPI extra needs it",
    "numpy": "image/OCR modes are off by default and fail with a clear error",
    "fonttools": "only the image-utils extra (font OCR) needs it",
    "brotli": "image-utils floor >=1.2.1 exceeds Chaquopy's 1.1.0 wheel",
}

#: Highest wheel version Chaquopy 16.1 offers for the compiled packages used
#: here (https://chaquo.com/pypi-13.1/, Python 3.12).  A ``>=``/``==`` floor
#: above the ceiling makes the APK build fail in ``pip``; keep the Gradle
#: install unpinned and let Chaquopy resolve the newest available wheel.
CHAQUOPY_WHEEL_CEILINGS: dict[str, Version] = {
    "aiohttp": Version("3.10.10"),
    "cryptography": Version("42.0.8"),
    "lxml": Version("5.3.0"),
    "pillow": Version("11.0.0"),
    "pycryptodome": Version("3.21.0"),
    "pyyaml": Version("6.0.3"),
}

#: Python 3.12 dropped every 32-bit ABI in Chaquopy.
CHAQUOPY_PY312_ABIS = {"arm64-v8a", "x86_64"}


def _normalize(name: str) -> str:
    """PEP 503 distribution name normalization."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirements(raw: list[str]) -> dict[str, Requirement]:
    """Parse requirement strings and key them by normalized distribution name."""
    parsed: dict[str, Requirement] = {}
    for item in raw:
        requirement = Requirement(item)
        parsed[_normalize(requirement.name)] = requirement
    return parsed


def _project() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))["project"]


def _declared(*extras: str) -> dict[str, Requirement]:
    """Project dependencies plus the requested optional extras."""
    project = _project()
    raw = list(project["dependencies"])
    for extra in extras:
        raw.extend(project["optional-dependencies"][extra])
    return _requirements(raw)


def _all_declared() -> dict[str, Requirement]:
    """Every dependency mentioned anywhere in ``[project]``."""
    project = _project()
    raw = list(project["dependencies"])
    for items in project["optional-dependencies"].values():
        raw.extend(items)
    return _requirements(raw)


def _chaquopy_block() -> str:
    """The text of the ``chaquopy { ... }`` block."""
    text = ANDROID_GRADLE.read_text(encoding="utf-8")
    start = text.index("chaquopy {")
    return text[start:]


def _chaquopy_installs() -> dict[str, Requirement]:
    """The ``install("...")`` entries, keyed by normalized distribution name."""
    matches = re.findall(r'install\(\s*"([^"]+)"\s*\)', _chaquopy_block())
    assert matches, "no install(...) entries found in the Chaquopy block"
    return _requirements(matches)


def _chaquopy_python_version() -> str:
    match = re.search(r'version\s*=\s*"(\d+\.\d+)"', _chaquopy_block())
    assert match is not None, "Chaquopy block has no Python version"
    return match.group(1)


def _build_python_paths() -> list[str]:
    """The literal path passed to ``buildPython``, if the build pins one."""
    block = _chaquopy_block()
    match = re.search(r"buildPython\s*=\s*listOf\(([^)]*)\)", block)
    if match is not None:
        quoted = re.findall(r'"([^"]+)"', match.group(1))
        if quoted:
            return quoted
    # The checked-in form is conditional:
    #   rootProject.file("../.venv/Scripts/python.exe")
    #       .takeIf { it.exists() }
    #       ?.let { buildPython = listOf(it.absolutePath) }
    match = re.search(
        r'rootProject\.file\(\s*"([^"]+)"\s*\)\s*\n?\s*\.takeIf[^\n]*\n?\s*\?\.let\s*\{\s*buildPython',
        block,
    )
    if match is not None:
        return [match.group(1)]
    return []


def _gradle_root() -> Path:
    """The Gradle root project directory (``android/``)."""
    return ANDROID_GRADLE.parents[1]


def _resolve_build_python(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = _gradle_root() / candidate
    return candidate.resolve()


def _abi_filters() -> list[str]:
    text = ANDROID_GRADLE.read_text(encoding="utf-8")
    match = re.search(r"abiFilters\s*\+=\s*listOf\(([^)]*)\)", text)
    assert match is not None, "ndk.abiFilters is missing"
    return re.findall(r'"([^"]+)"', match.group(1))


def _workflow_python_version(path: Path) -> str:
    """The top-level ``PYTHON_VERSION`` env value of a workflow."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^\s*PYTHON_VERSION:\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, f"{path.name} has no PYTHON_VERSION"
    return match.group(1)


def _module_level_imports(root: Path) -> dict[str, set[Path]]:
    """Third-party modules imported at module level, excluding typing blocks."""
    found: dict[str, set[Path]] = {}

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self._record(alias.name.split(".")[0], node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level == 0 and node.module:
                self._record(node.module.split(".")[0], node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_If(self, node: ast.If) -> None:
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                return
            self.generic_visit(node)

        def _record(self, module: str, node: ast.AST) -> None:
            if (
                module in sys.stdlib_module_names
                or module == "novel_downloader"
                or module == "__future__"
            ):
                return
            path = Path(getattr(node, "_source_path", root))
            found.setdefault(module, set()).add(path)

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            node._source_path = path  # type: ignore[attr-defined]
        Visitor().visit(tree)
    return found


# ---------------------------------------------------------------------------
# Declared metadata
# ---------------------------------------------------------------------------


def test_music_extras_declare_every_module_level_import() -> None:
    """pyproject must declare the vendored adapters' hard imports.

    This is the desktop half of the contract: if a platform module gains a new
    module-level import, ``pip install ".[music]"`` must install it too.
    """
    declared = _declared("music")
    missing: dict[str, str] = {}
    for module, sources in _module_level_imports(MUSIC_TREE).items():
        dist = MODULE_TO_DIST.get(module, _normalize(module))
        if dist not in declared:
            first = sorted(sources)[0].relative_to(ROOT)
            missing[dist] = f"{first} imports {module!r}"
    assert not missing, (
        "music extras are missing hard imports of the vendored adapters: "
        + ", ".join(f"{dist} ({why})" for dist, why in sorted(missing.items()))
    )


def test_music_extra_is_the_runtime_set() -> None:
    """Guard against an accidental rewrite of the extras list."""
    declared = set(_declared("music"))
    assert declared >= MUSIC_RUNTIME_DISTS, (
        f"music extra lost {sorted(MUSIC_RUNTIME_DISTS - declared)}"
    )


# ---------------------------------------------------------------------------
# Chaquopy install list
# ---------------------------------------------------------------------------


def test_chaquopy_covers_core_and_music_dependencies() -> None:
    installed = _chaquopy_installs()
    required = set(_declared("music"))
    excluded = set(ANDROID_NOT_INSTALLED)

    missing = required - set(installed) - excluded
    assert not missing, (
        "Chaquopy does not install dependencies the Android engine needs: "
        + ", ".join(sorted(missing))
    )


def test_android_exclusions_are_required_and_documented() -> None:
    """The exclusion map may only shrink the set that would otherwise be required."""
    declared_anywhere = set(_all_declared())
    installed = _chaquopy_installs()

    stale = set(ANDROID_NOT_INSTALLED) - declared_anywhere
    assert not stale, (
        "ANDROID_NOT_INSTALLED lists distributions no longer required: "
        + ", ".join(sorted(stale))
    )
    installed_anyway = set(ANDROID_NOT_INSTALLED) & set(installed)
    assert not installed_anyway, (
        "Android must not install packages without a usable wheel: "
        + ", ".join(sorted(installed_anyway))
    )


def test_orjson_gap_is_guarded_at_registration_time() -> None:
    """QQ must be hidden by find_spec, not registered and broken on first use."""
    from novel_downloader.apps.music.providers import musicdl

    guarded = {
        _normalize(dependency)
        for dependencies in musicdl.PLATFORM_DEPS.values()
        for dependency in dependencies
    }
    assert "orjson" in guarded, (
        "QQ imports orjson at module load; it must be listed in "
        "musicdl.PLATFORM_DEPS so Android does not register a broken platform"
    )
    assert "qq" in musicdl.PLATFORM_DEPS
    # Desktop parity: the distribution stays in the music extra, so a clean
    # `pip install ".[music]"` still enables QQ.
    assert "orjson" in _declared("music")


def test_platform_deps_are_described_in_capabilities() -> None:
    """The capability matrix must explain every find_spec-hidden platform."""
    from novel_downloader.apps.music import providers
    from novel_downloader.apps.music.providers import musicdl

    for key, dependencies in musicdl.PLATFORM_DEPS.items():
        capability = providers.capability(key)
        described = {
            item.name for item in capability.requirements if item.kind == "python"
        }
        assert set(dependencies) <= described, (
            f"{key} hides on missing {sorted(set(dependencies) - described)} "
            "without telling the user why"
        )


def test_chaquopy_install_list_has_no_duplicates() -> None:
    entries = re.findall(r'install\(\s*"([^"]+)"\s*\)', _chaquopy_block())
    names = [_normalize(Requirement(entry).name) for entry in entries]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"duplicate install() entries: {sorted(duplicates)}"


# ---------------------------------------------------------------------------
# Version / interpreter / ABI alignment
# ---------------------------------------------------------------------------


def test_chaquopy_python_matches_ci_and_requires_python() -> None:
    version = _chaquopy_python_version()
    assert _workflow_python_version(CI_YML) == version
    assert _workflow_python_version(BUILD_YML) == version

    requires = SpecifierSet(_project()["requires-python"])
    assert Version(version) in requires, (
        f"Chaquopy runs Python {version}, outside requires-python {requires}"
    )
    # Chaquopy 3.12 needs per-App ABI builds; 32-bit ABIs must stay out.
    min_sdk = re.search(r"minSdk\s*=\s*(\d+)", ANDROID_GRADLE.read_text())
    assert min_sdk is not None and int(min_sdk.group(1)) >= 24


def test_build_python_interpreter_matches_chaquopy() -> None:
    """When the repo venv is used, it must be the same minor version."""
    version = _chaquopy_python_version()
    paths = _build_python_paths()
    assert paths, "buildPython should point at the repository venv"

    resolved = [_resolve_build_python(path) for path in paths]
    existing = [path for path in resolved if path.is_file()]
    if not existing:
        pytest.skip("the declared buildPython does not exist on this platform")

    output = subprocess.run(
        [
            str(existing[0]),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert output.stdout.strip() == version, (
        f"buildPython is {output.stdout.strip()}, Chaquopy expects {version}"
    )


def test_chaquopy_abis_are_supported() -> None:
    filters = _abi_filters()
    assert filters, "abiFilters must be explicit for Chaquopy"
    assert set(filters) == CHAQUOPY_PY312_ABIS
    assert set(filters) <= CHAQUOPY_PY312_ABIS


# ---------------------------------------------------------------------------
# Version pin conflicts
# ---------------------------------------------------------------------------


def _floor(requirement: Requirement) -> Version | None:
    floors = [
        Version(spec.version)
        for spec in requirement.specifier
        if spec.operator in {">=", "==", "~="}
    ]
    return max(floors) if floors else None


def test_chaquopy_install_pins_fit_known_wheel_ceilings() -> None:
    """A floor copied from pyproject must not exceed Chaquopy's newest wheel."""
    installed = _chaquopy_installs()
    offenders: list[str] = []
    for dist, ceiling in CHAQUOPY_WHEEL_CEILINGS.items():
        requirement = installed.get(dist)
        if requirement is None:
            continue
        floor = _floor(requirement)
        if floor is not None and floor > ceiling:
            offenders.append(f"{dist} {floor} > Chaquopy {ceiling}")
    assert not offenders, "APK pip resolution would fail: " + "; ".join(offenders)


def test_pillow_desktop_floor_is_a_documented_android_gap() -> None:
    """Pillow is the one place Android knowingly trails the desktop floor.

    pyproject requires Pillow >=13 (supply-chain floor), but Chaquopy 16.1
    only ships 11.0.0 for cp312.  The Gradle install must therefore stay
    unpinned; adding the desktop floor there breaks the APK build, removing
    the floor from pyproject weakens the desktop security posture.
    """
    desktop_floor = _floor(_declared("music")["pillow"])
    assert desktop_floor is not None
    assert desktop_floor > CHAQUOPY_WHEEL_CEILINGS["pillow"]
    assert _floor(_chaquopy_installs()["pillow"]) is None, (
        "do not copy the Pillow >=13 floor into the Chaquopy install list: "
        "the repository only offers 11.0.0 and pip would fail to resolve"
    )


def test_pywidevine_pin_matches_the_chaquopy_pycryptodome_ceiling() -> None:
    """pywidevine 1.9 needs pycryptodome >=3.23, which Chaquopy cannot ship."""
    installed = _chaquopy_installs()
    pywidevine = installed["pywidevine"]
    assert pywidevine.specifier.contains("1.8.0")
    assert not pywidevine.specifier.contains("1.9.0"), (
        "pywidevine 1.9 requires pycryptodome>=3.23; Chaquopy caps it at 3.21.0"
    )
    protobuf = installed["protobuf"]
    assert protobuf.specifier.contains("4.25.8")
    assert not protobuf.specifier.contains("6.33.0"), (
        "pywidevine 1.8's generated bindings need protobuf <5"
    )
    assert CHAQUOPY_WHEEL_CEILINGS["pycryptodome"] < Version("3.23.0")


def test_android_runtime_does_not_import_the_excluded_packages() -> None:
    """Exclusions are for optional extras, never for module-level imports."""
    hard_imports = {
        MODULE_TO_DIST.get(module, _normalize(module))
        for module in _module_level_imports(MUSIC_TREE)
    }
    overlap = hard_imports & set(ANDROID_NOT_INSTALLED)
    assert overlap <= {"orjson"}, (
        "these packages are imported unconditionally but excluded from the "
        f"APK without a guard: {sorted(overlap - {'orjson'})}"
    )
