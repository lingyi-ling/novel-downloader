#!/usr/bin/env python3
"""
Shared PyInstaller collection helpers for the novel-cli / novel-api specs.

The site plugins live in namespace-style directories (no ``__init__.py``) and
are imported by name at runtime, so PyInstaller's static analysis and even
``collect_submodules`` cannot see them.  Walking the source tree is the only
reliable way to produce the hidden imports.  The same applies to the vendored
musicdl package, whose submodules are imported lazily by platform key and
which cannot be fully imported at build time (some optional dependencies such
as ``m3u8`` are intentionally absent).

The helper is imported from both specs; PyInstaller executes specs with the
spec directory available via ``SPECPATH``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

PACKAGE = "novel_downloader"

#: (source-relative directory, importable package name) pairs whose modules
#: are selected at runtime rather than by a regular ``import`` statement.
DYNAMIC_TREES: tuple[tuple[str, str], ...] = (
    ("plugins/sites", f"{PACKAGE}.plugins.sites"),
    ("plugins/processors", f"{PACKAGE}.plugins.processors"),
    ("plugins/rule", f"{PACKAGE}.plugins.rule"),
    (
        "apps/music/providers/_vendor/musicdl",
        f"{PACKAGE}.apps.music.providers._vendor.musicdl",
    ),
)

#: Modules imported dynamically by string (importlib.resources / lazy imports).
EXTRA_HIDDEN_IMPORTS: tuple[str, ...] = (
    # ``importlib.resources.files("novel_downloader.locales")``
    f"{PACKAGE}.locales",
    # ``importlib.resources.files("novel_downloader.resources")``
    f"{PACKAGE}.resources",
    # ``from novel_downloader.apps.browser import ...`` inside site searchers
    f"{PACKAGE}.apps.browser",
    # ``from novel_downloader.apps.login import ...`` inside the login command
    f"{PACKAGE}.apps.login",
)


#: Development-only, removed-UI and unused heavy packages.  Keeping these out
#: keeps the bundles small; none of them is reachable from novel-cli/novel-api.
EXCLUDES: tuple[str, ...] = (
    # Development tooling
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "pytest_aiohttp",
    "pytest_cov",
    "pytest_mock",
    "mypy",
    "mypy_extensions",
    "ruff",
    "coverage",
    "babel",
    "mkdocs",
    "mkdocs_material",
    "mkdocstrings",
    "pymdownx",
    "material",
    # Removed desktop/web UI (the API no longer serves a web frontend)
    "nicegui",
    "customtkinter",
    # Notebook / scientific stacks not used by the downloader runtime
    "matplotlib",
    "pandas",
    "scipy",
    "IPython",
    "jupyter",
    # Not reachable from the registry (kept in-tree for reference only)
    f"{PACKAGE}.plugins.archived",
)


def modules_from_tree(package_dir: Path, package_name: str) -> list[str]:
    """
    Return every importable module name under ``package_dir``.

    Directories without ``__init__.py`` are treated as namespace packages; the
    resulting names still resolve at runtime (PyInstaller records the parent
    directories as PEP 420 namespace packages).

    :param package_dir: Absolute path of the package directory on disk.
    :param package_name: Fully qualified package name for that directory.
    :return: Sorted, de-duplicated module names.
    """
    modules = {package_name}
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir).with_suffix("")
        parts = [part for part in relative.parts if part != "__init__"]
        modules.add(".".join([package_name, *parts]) if parts else package_name)
    return sorted(modules)


def _iter_non_python_files(package_dir: Path) -> Iterator[Path]:
    """Yield data files (not source/bytecode) below ``package_dir``."""
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in {".py", ".pyc", ".pyo"}:
            continue
        yield path


def project_datas(src_dir: Path) -> list[tuple[str, str]]:
    """
    Build the ``datas`` list for runtime resources shipped with the package.

    Collected payloads:

    * ``locales/**`` (``.mo``, ``.po`` and the template),
    * ``resources/**`` (icons, CSS, JS, JSON maps, sample config),
    * ``plugins/rule/builtin/*.json``,
    * ``py.typed`` and the vendored musicdl ``SOURCE.txt``.

    The site plugin ``.py`` files are copied alongside their compiled modules
    on purpose: ``importlib.resources.contents()`` in the plugin registry
    enumerates site directories on disk, and PyInstaller does not reconstruct
    directories that contain only source files.  Keeping the sources also
    provides a filesystem fallback if a namespace module ever misses the PYZ.
    """
    package_dir = src_dir / PACKAGE
    datas: list[tuple[str, str]] = []
    for path in _iter_non_python_files(package_dir):
        # PyInstaller's ``datas`` destination is a directory; the source
        # basename is appended automatically.
        relative = path.relative_to(package_dir)
        datas.append((str(path), str(Path(PACKAGE) / relative.parent)))

    sites_dir = package_dir / "plugins" / "sites"
    for path in sorted(sites_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir)
        datas.append((str(path), str(Path(PACKAGE) / relative.parent)))
    return datas


def common_hidden_imports(src_dir: Path) -> list[str]:
    """Return all hidden imports required by the CLI and the API."""
    package_dir = src_dir / PACKAGE
    hidden: list[str] = list(EXTRA_HIDDEN_IMPORTS)
    for relative, package_name in DYNAMIC_TREES:
        hidden.extend(modules_from_tree(package_dir / relative, package_name))
    return sorted(set(hidden))


def collect_common(src_dir: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Return ``(datas, hiddenimports)`` shared by both specifications.

    :param src_dir: Repository ``src/`` directory.
    """
    return project_datas(src_dir), common_hidden_imports(src_dir)
