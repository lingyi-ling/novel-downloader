# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone ``novel-cli`` console application (onedir).

Build via the wrapper (recommended)::

    .venv/Scripts/python.exe scripts/build_cli.py --target cli

or directly::

    .venv/Scripts/python.exe -m PyInstaller packaging/novel_cli.spec \
        --noconfirm --clean

The bundle is intentionally onedir: it starts faster than one-file mode, and
the data-heavy resources (locales, site plugins, vendored musicdl) stay next to
the executable instead of being unpacked on every launch.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "novel-cli"

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
for extra_path in (SPEC_DIR, SRC_DIR):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))

from _spec_common import EXCLUDES, collect_common  # noqa: E402

ICON_PATH = SRC_DIR / "novel_downloader" / "resources" / "icons" / "app.ico"

datas, hiddenimports = collect_common(SRC_DIR)

a = Analysis(
    [str(SPEC_DIR / "cli_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDES),
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
