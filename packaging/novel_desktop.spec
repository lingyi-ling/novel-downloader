# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Novel Downloader desktop application (onedir).

Build it with::

    .venv/Scripts/python.exe -m PyInstaller packaging/novel_desktop.spec \
        --noconfirm --clean

or via the wrapper (recommended)::

    .venv/Scripts/python.exe scripts/build_desktop.py

Why onedir: NiceGUI ships large static assets and pywebview loads .NET/WebView2
assemblies from its package at runtime. One-file mode would unpack all of that
on every launch and is noticeably slower and more fragile; onedir keeps the
files next to the executable.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)

APP_NAME = "NovelDownloader"

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

ICON_PATH = SRC_DIR / "novel_downloader" / "resources" / "icons" / "app.ico"


def _modules_from_tree(package_dir: Path, package_name: str) -> list[str]:
    """
    List importable module names by walking ``package_dir`` on disk.

    The site plugins are namespace-style directories without ``__init__.py``,
    so ``collect_submodules`` / ``pkgutil`` cannot see them. The same applies
    to modules that fail to import at build time because of optional
    third-party dependencies. Walking the tree avoids both problems.
    """
    modules: list[str] = [package_name]
    for path in sorted(package_dir.rglob("*.py")):
        rel = path.relative_to(package_dir).with_suffix("")
        if any(part == "__pycache__" for part in rel.parts):
            continue
        parts = [part for part in rel.parts if part != "__init__"]
        modules.append(".".join([package_name, *parts]) if parts else package_name)
    return modules


# --------------------------------------------------------------------------- #
# Third-party runtime essentials
# --------------------------------------------------------------------------- #
datas = []
binaries = []
hiddenimports = []

# NiceGUI: static assets (Vue, ESM libs), templates, scripts and the native
# window bridge. The testing helpers pull pytest in and are never used here.
nicegui_datas, nicegui_binaries, nicegui_hidden = collect_all(
    "nicegui",
    filter_submodules=lambda name: ".testing" not in name,
)
datas += nicegui_datas
binaries += nicegui_binaries
hiddenimports += nicegui_hidden

# pywebview: platform backends (winforms/edgechromium on Windows) plus the
# bundled WebView2/.NET interop DLLs under webview/lib.
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
datas += webview_datas
binaries += webview_binaries
hiddenimports += webview_hidden

# pythonnet ships its own PyInstaller hook; add it explicitly so the
# Python.Runtime.dll and System.* references are collected deterministically.
try:
    import pythonnet

    pythonnet_hook_dir = Path(pythonnet.__file__).resolve().parent / "_pyinstaller"
except Exception:  # pragma: no cover - pythonnet is a hard pywebview dep on Windows
    pythonnet_hook_dir = None

# --------------------------------------------------------------------------- #
# Project data and dynamically imported modules
# --------------------------------------------------------------------------- #
# locales (*.mo/*.po), resources (icons/css/js/json/toml), rule/builtin/*.json,
# py.typed and the vendored musicdl SOURCE.txt.
datas += collect_data_files("novel_downloader")

# Site plugins are imported at runtime via importlib from
# ``plugins.sites.<key>.<kind>``; they are invisible to static analysis.
hiddenimports += _modules_from_tree(
    SRC_DIR / "novel_downloader" / "plugins" / "sites",
    "novel_downloader.plugins.sites",
)

# Rule-driven sources are imported lazily from the registry.
hiddenimports += collect_submodules("novel_downloader.plugins.rule")

# Processors are also resolved dynamically (``plugins.processors.<key>``).
hiddenimports += collect_submodules("novel_downloader.plugins.processors")

# Vendored musicdl backends, imported by name from the provider adapter. Walk
# the tree so modules with missing optional deps at build time are still
# bundled (their optional imports simply stay unavailable at runtime).
hiddenimports += _modules_from_tree(
    SRC_DIR
    / "novel_downloader"
    / "apps"
    / "music"
    / "providers"
    / "_vendor"
    / "musicdl",
    "novel_downloader.apps.music.providers._vendor.musicdl",
)

# Imported inside functions / conditionally, so list them explicitly.
hiddenimports += [
    "novel_downloader.apps.desktop.main",
    "novel_downloader.apps.web.pages",
    "novel_downloader.apps.browser",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
]

# --------------------------------------------------------------------------- #
# Exclusions: development-only and unused heavy dependencies
# --------------------------------------------------------------------------- #
EXCLUDES = [
    # Test / lint / docs tooling
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
    "playwright",
    "selenium",
    "mkdocs",
    "mkdocs_material",
    "mkdocstrings",
    "pymdownx",
    "material",
    "babel",
    # Desktop UI does not use Tk; vendored music backends only touch it in
    # optional code paths (screen-size probing).
    "tkinter",
    "_tkinter",
    "customtkinter",
    # Scientific / notebook stacks are not used by the web or desktop UI.
    "matplotlib",
    "pandas",
    "scipy",
    "IPython",
    "jupyter",
    # Not reachable from the registry (kept in-tree for reference only).
    "novel_downloader.plugins.archived",
]

a = Analysis(
    [str(SPEC_DIR / "desktop_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=(
        [str(pythonnet_hook_dir)]
        if pythonnet_hook_dir and pythonnet_hook_dir.is_dir()
        else []
    ),
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
