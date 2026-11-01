# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone ``novel-api`` console application (onedir).

Build via the wrapper (recommended)::

    .venv/Scripts/python.exe scripts/build_cli.py --target api

or directly::

    .venv/Scripts/python.exe -m PyInstaller packaging/novel_api.spec \
        --noconfirm --clean

Uvicorn and FastAPI select protocol/loop implementations dynamically, so their
submodules are collected explicitly (in addition to PyInstaller's own hooks).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "novel-api"

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
for extra_path in (SPEC_DIR, SRC_DIR):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))

from _spec_common import CONSOLE_EXCLUDES, EXCLUDES, collect_common  # noqa: E402

ICON_PATH = SRC_DIR / "novel_downloader" / "resources" / "icons" / "app.ico"

datas, hiddenimports = collect_common(SRC_DIR)
# uvicorn's hook already does this, but explicit is safer across hook versions.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    [str(SPEC_DIR / "api_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[*EXCLUDES, *CONSOLE_EXCLUDES],
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
    console=True,
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
