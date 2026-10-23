# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the native Tkinter desktop client (onedir, windowed).

Build via the wrapper (recommended)::

    .venv/Scripts/python.exe scripts/build_desktop.py --clean-dist

or directly::

    .venv/Scripts/python.exe -m PyInstaller packaging/novel_desktop.spec \
        --noconfirm --clean

Differences from ``novel_cli.spec``:

* ``console=False`` so launching the bundle does not flash a console window.
  PyInstaller then also installs its windowed traceback handler, which is why
  ``disable_windowed_traceback`` stays ``False``: a crash during start-up is
  reported in a dialog instead of vanishing.
* Tkinter lives in the standard library and is imported lazily by ``ui.py``,
  so every widget module is listed as a hidden import and PyInstaller's own
  ``_tkinter`` hook then pulls in the Tcl/Tk data files and DLLs.

The bundle stays onedir for the same reason as the CLI: the data-heavy
resources (locales, site plugins, vendored musicdl) are read from disk at
runtime and should not be unpacked on every launch.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "NovelDownloader"

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
for extra_path in (SPEC_DIR, SRC_DIR):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))

from PyInstaller.utils.hooks import collect_data_files  # noqa: E402

from _spec_common import EXCLUDES, collect_common  # noqa: E402

# The icon is taken from the packaged resources module so that renaming or
# moving the asset cannot silently produce an icon-less bundle.
try:  # noqa: SIM105 - the fallback is the point
    from novel_downloader.infra.paths import APP_ICON_PATH

    ICON_PATH = Path(str(APP_ICON_PATH))
except Exception:  # noqa: BLE001 - fall back to the source tree layout
    ICON_PATH = SRC_DIR / "novel_downloader" / "resources" / "icons" / "app.ico"

#: Standard-library Tk modules.  ``tkinter`` is imported from ``ui.py`` via a
#: deferred ``from .ui import run_app`` in ``main``; listing the widget modules
#: explicitly guarantees the extension module ``_tkinter`` (and therefore
#: PyInstaller's ``hook-_tkinter``, which ships the Tcl/Tk runtime) is collected
#: even if a future refactor hides the import from the analyser.
TKINTER_MODULES: tuple[str, ...] = (
    "tkinter",
    "tkinter.colorchooser",
    "tkinter.commondialog",
    "tkinter.constants",
    "tkinter.dialog",
    "tkinter.filedialog",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.scrolledtext",
    "tkinter.simpledialog",
    "tkinter.ttk",
)

datas, hiddenimports = collect_common(SRC_DIR)

# ``collect_data_files`` is the canonical PyInstaller collection (locales,
# resources, builtin rules, py.typed, the vendored musicdl SOURCE.txt); the
# hand-rolled tree walk in ``_spec_common`` covers the same ground plus the
# site-plugin *sources*, which the plugin registry enumerates from disk.  Both
# are merged here and de-duplicated so the COLLECT step never sees the same
# target twice.
_merged_datas: list[tuple[str, str]] = []
_seen_datas: set[tuple[str, str]] = set()
for entry in (*datas, *collect_data_files("novel_downloader")):
    if entry in _seen_datas:
        continue
    _seen_datas.add(entry)
    _merged_datas.append(entry)
datas = _merged_datas

hiddenimports = sorted({*hiddenimports, *TKINTER_MODULES})

a = Analysis(
    [str(SPEC_DIR / "desktop_entry.py")],
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