#!/usr/bin/env python3
"""
profile_desktop_startup
-----------------------

Measure the Windows desktop client's cold-start path without opening a real
window.

Each phase runs in a fresh interpreter: a warm process would report a fraction
of the real cost and hide exactly the imports this tool exists to find.  A
lightweight stand-in for ``webview`` records the moment the window would have
been created, so the measurement needs no display and no pywebview backend.

Phases reported by ``phases`` mode:

* ``import_main``      - ``novel_downloader.apps.desktop.main`` import
* ``prepare_home``     - app-home resolution, ``chdir``, config seeding
* ``setup_logging``    - rotating file log installation
* ``import_webview``   - the pywebview host module and its imports
* ``config_load``      - ``settings.toml`` parse (on demand, not startup)
* ``bridge_init``      - ``ApiBridge()`` construction
* ``services_start``   - task-queue restore (engine thread + manager)
* ``window_ready``     - saved geometry + (stubbed) window + GUI loop start

``window`` mode follows the real entry point instead: it imports the modules,
calls :func:`novel_downloader.apps.desktop.webview_app.run_webview_app` once
and then waits (bounded) for the background task-queue restore to finish.  The
gap between ``window_ready`` and ``services_done`` shows how much of the
queue restore still blocks the first paint.

``--frozen`` measures the PyInstaller onedir bundle instead of the source
tree.  The desktop executable is windowed (no console and no ``--version``),
so a Win32 probe watches for the visible main window and records the moment
it appears.  Native module loads, process CPU/I/O counters and the app's own
log line split that wall time into bootloader, imports and GUI phases.  A
py-spy sample (when available) attributes the main thread's work, and PYZ
extraction costs show what the frozen import machinery has to decompress.

Usage::

    .venv/Scripts/python.exe scripts/profile_desktop_startup.py
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --json-out before.json
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --compare before.json
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --importtime
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --frozen
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MARKER = "__STARTUP_PROFILE_JSON__"

#: Phase order used by the report (phases mode).
PHASES: tuple[str, ...] = (
    "import_main",
    "prepare_home",
    "setup_logging",
    "import_webview",
    "config_load",
    "bridge_init",
    "services_start",
    "window_ready",
)

#: Phases that gate the first paint.  ``config_load`` and ``services_start``
#: are reported for context but run on demand (or in the background) and are
#: deliberately excluded from the window-path total.
WINDOW_PATH_PHASES: tuple[str, ...] = (
    "import_main",
    "prepare_home",
    "setup_logging",
    "import_webview",
    "bridge_init",
    "window_ready",
)

#: Modules that must not be imported before the window exists.  ``aiohttp``
#: arrives with the task manager, ``webview`` only when the GUI starts.
DEFERRED_MODULES: tuple[str, ...] = (
    "aiohttp",
    "novel_downloader.apps.core.task_manager",
    "novel_downloader.plugins.rule",
    "urllib.request",
)

#: Title pywebview gives the desktop window (see ``webview_app``).
DESKTOP_WINDOW_TITLE = "书音 BookTone"

#: Source-tree commands used for the like-for-like comparisons.
SOURCE_DESKTOP_CODE = (
    "from novel_downloader.apps.desktop.main import desktop_main; desktop_main()"
)
SOURCE_CLI_CODE = "from novel_downloader.apps.cli.main import cli_main; cli_main()"

#: Frozen artifacts produced by ``scripts/build_desktop.py`` / ``build_cli.py``.
FROZEN_DESKTOP_EXE = ROOT / "dist" / "NovelDownloader" / "NovelDownloader.exe"
FROZEN_CLI_EXE = ROOT / "dist" / "novel-cli" / "novel-cli.exe"
FROZEN_PYZ = ROOT / "build" / "novel_desktop" / "PYZ-00.pyz"
FROZEN_REPORT_PATH = ROOT / "build" / "frozen-startup.json"

#: ``%Y-%m-%d %H:%M:%S,mmm`` as written by the desktop's rotating log.
_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")


class _Signal:
    """Minimal pywebview event stand-in supporting ``handler += callback``."""

    def __iadd__(self, _handler: Any) -> _Signal:
        return self


class _Events:
    def __init__(self) -> None:
        self.closing = _Signal()
        self.closed = _Signal()


class _Window:
    width = 1220
    height = 800
    x = 0
    y = 0

    def __init__(self) -> None:
        self.events = _Events()


def _install_fake_webview() -> None:
    """Install a ``webview`` module whose ``start`` returns immediately."""
    module = ModuleType("webview")

    def create_window(*_args: Any, **_kwargs: Any) -> _Window:
        return _Window()

    def start(*_args: Any, **_kwargs: Any) -> None:
        return None

    module.create_window = create_window  # type: ignore[attr-defined]
    module.start = start  # type: ignore[attr-defined]
    sys.modules["webview"] = module


def _elapsed(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _child_phases() -> int:
    """Run one detailed cold-start measurement and print it as JSON."""
    phases: dict[str, float] = {}
    imported: dict[str, bool] = {}

    mark = time.perf_counter()
    import novel_downloader.apps.desktop.main as main_mod

    phases["import_main"] = _elapsed(mark)

    mark = time.perf_counter()
    main_mod._prepare_home()
    phases["prepare_home"] = _elapsed(mark)

    mark = time.perf_counter()
    from novel_downloader.infra.paths import app_home

    main_mod._setup_file_logging(app_home())
    phases["setup_logging"] = _elapsed(mark)

    mark = time.perf_counter()
    from novel_downloader.apps.desktop import webview_app

    phases["import_webview"] = _elapsed(mark)

    for name in DEFERRED_MODULES:
        imported[name] = name in sys.modules

    mark = time.perf_counter()
    from novel_downloader.infra.config import load_config

    load_config()
    phases["config_load"] = _elapsed(mark)

    mark = time.perf_counter()
    bridge = webview_app.ApiBridge()
    phases["bridge_init"] = _elapsed(mark)

    mark = time.perf_counter()
    bridge.start_services()
    phases["services_start"] = _elapsed(mark)

    _install_fake_webview()
    mark = time.perf_counter()
    webview_app.run_webview_app()
    phases["window_ready"] = _elapsed(mark)

    from novel_downloader.plugins.registry import registrar

    payload = {
        "mode": "phases",
        "phases": phases,
        "deferred_imported": imported,
        "registered_plugins_after_window": len(registrar._searchers),
    }
    print(MARKER + json.dumps(payload))
    return 0


def _child_window() -> int:
    """Measure the real entry point (stubbed window) in a fresh process."""
    start = time.perf_counter()
    phases: dict[str, float] = {}

    mark = time.perf_counter()
    import novel_downloader.apps.desktop.main as main_mod

    phases["import_main"] = _elapsed(mark)

    mark = time.perf_counter()
    main_mod._prepare_home()
    phases["prepare_home"] = _elapsed(mark)

    mark = time.perf_counter()
    from novel_downloader.infra.paths import app_home

    main_mod._setup_file_logging(app_home())
    phases["setup_logging"] = _elapsed(mark)

    mark = time.perf_counter()
    from novel_downloader.apps.desktop import webview_app

    phases["import_webview"] = _elapsed(mark)

    _install_fake_webview()

    loaded_before = {name: name in sys.modules for name in DEFERRED_MODULES}
    window_modules = sorted(sys.modules)

    mark = time.perf_counter()
    webview_app.run_webview_app()
    phases["window_ready"] = _elapsed(mark)
    window_ready_total = _elapsed(start)

    # Only the queue restore is allowed to finish after the window exists; the
    # extra wait is what the old blocking call used to add to first paint.
    wait_mark = time.perf_counter()
    deadline = time.monotonic() + 15.0
    restored = False
    while time.monotonic() < deadline:
        module = sys.modules.get("novel_downloader.apps.core.task_manager")
        manager = getattr(module, "manager", None)
        if module is not None and getattr(manager, "_started", False):
            restored = True
            break
        time.sleep(0.01)
    phases["services_done"] = _elapsed(wait_mark)
    services_done_total = _elapsed(start)

    payload = {
        "mode": "window",
        "phases": phases,
        "window_ready_total_ms": window_ready_total,
        "services_done_total_ms": services_done_total,
        "deferred_before_window": loaded_before,
        "services_restored": restored,
        # Module names imported before the window, used to size the frozen
        # PYZ decompression cost of the same start-up path.  ``modules`` is
        # sampled after the queue restore for comparison.
        "window_modules": window_modules,
        "modules": sorted(sys.modules),
    }
    print(MARKER + json.dumps(payload))
    return 0


def _child_main(argv: list[str]) -> int:
    if argv[0] == "phases":
        return _child_phases()
    if argv[0] == "window":
        return _child_window()
    raise SystemExit(f"unknown child mode: {argv[0]}")


def _child_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["NOVEL_HOME"] = str(home)
    env["NOVEL_MUSIC_HOME"] = str(home / "music")
    # An inherited PYTHONPATH can point at a different checkout; keep the
    # repository's src first so the measurement describes this tree.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return env


def _run_child(mode: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="noveldl-startup-") as temp:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", mode],
            cwd=ROOT,
            env=_child_env(Path(temp)),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    if proc.returncode != 0:
        raise SystemExit(
            f"child mode {mode!r} failed ({proc.returncode}):\n{proc.stderr}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER) :])  # type: ignore[no-any-return]
    raise SystemExit(f"child mode {mode!r} produced no report:\n{proc.stdout}")


def _median_phases(runs: list[dict[str, Any]]) -> dict[str, float]:
    keys = sorted({key for run in runs for key in run["phases"]})
    return {
        key: statistics.median(
            run["phases"][key] for run in runs if key in run["phases"]
        )
        for key in keys
    }


def _print_table(
    title: str,
    values: dict[str, float],
    baseline: dict[str, float] | None,
    order: tuple[str, ...] = PHASES,
) -> None:
    print(f"\n{title}")
    header = f"{'phase':<22}{'median ms':>10}"
    if baseline is not None:
        header += f"{'before ms':>11}{'delta ms':>11}"
    print(header)
    print("-" * len(header))
    for key in order:
        if key not in values:
            continue
        row = f"{key:<22}{values[key]:>10.1f}"
        if baseline is not None:
            before = baseline.get(key, 0.0)
            row += f"{before:>11.1f}{values[key] - before:>+11.1f}"
        print(row)
    total = sum(values[key] for key in WINDOW_PATH_PHASES if key in values)
    print("-" * len(header))
    print(f"{'window path total':<22}{total:>10.1f}")
    if baseline is not None:
        before_total = sum(
            baseline[key] for key in WINDOW_PATH_PHASES if key in baseline
        )
        print(f"{'window path delta':<22}{total - before_total:>+10.1f}")


def _importtime_rows() -> list[tuple[int, str]]:
    """Return ``(cumulative_us, module)`` rows for the window-path imports."""
    code = (
        "import novel_downloader.apps.desktop.main; "
        "import novel_downloader.apps.desktop.webview_app"
    )
    with tempfile.TemporaryDirectory(prefix="noveldl-importtime-") as temp:
        env = _child_env(Path(temp))
        proc = subprocess.run(
            [sys.executable, "-X", "importtime", "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    rows: list[tuple[int, str]] = []
    for line in proc.stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[1].strip()), parts[2].strip()))
        except ValueError:
            continue
    return rows


def _importtime_report() -> None:
    """Show the slowest cumulative imports for the window path."""
    rows = _importtime_rows()
    print("\nSlowest cumulative imports (us)")
    for cumulative, name in sorted(rows)[-15:]:
        print(f"{cumulative / 1000.0:>10.1f} ms  {name}")
    total = max((value for value, _name in rows), default=0) / 1000.0
    print(f"\nTOTAL (root import) {total:.1f} ms")


def _package_version() -> str:
    """Version declared by the source tree (the authority for the build)."""
    init_py = ROOT / "src" / "novel_downloader" / "__init__.py"
    text = init_py.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _exe_version(path: Path) -> str | None:
    """Read the Windows ``VS_FIXEDFILEINFO`` file version of ``path``."""
    import ctypes
    from ctypes import wintypes

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL

    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(
        buffer, "\\", ctypes.byref(value), ctypes.byref(length)
    ):
        return None
    fixed = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
    ms = fixed.dwFileVersionMS
    ls = fixed.dwFileVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _versions_match(exe_version: str | None, package_version: str) -> bool:
    if not exe_version:
        return False
    return exe_version == package_version or exe_version.startswith(
        package_version + "."
    )


class _WindowProbe:
    """Find the visible pywebview top-level window belonging to any process."""

    def __init__(self, title: str) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._title = title
        self._hits: list[dict[str, Any]] = []
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        ]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )(self._on_window)

    def _on_window(self, hwnd: Any, _lparam: Any) -> int:
        user32 = self._user32
        if not user32.IsWindowVisible(hwnd):
            return 1
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return 1
        title_buffer = self._ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        title = title_buffer.value
        class_buffer = self._ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        window_class = class_buffer.value
        if title != self._title and "WindowsForms" not in window_class:
            return 1
        pid = self._wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        rect = self._wintypes.RECT()
        user32.GetWindowRect(hwnd, self._ctypes.byref(rect))
        self._hits.append(
            {
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "title": title,
                "class": window_class,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
            }
        )
        return 1

    def poll(self) -> list[dict[str, Any]]:
        self._hits = []
        self._user32.EnumWindows(self._callback, 0)
        return list(self._hits)

    def close(self, hit: dict[str, Any]) -> None:
        """Ask a top-level window to close (``WM_CLOSE``)."""
        self._user32.PostMessageW(int(hit["hwnd"]), 0x0010, 0, 0)


def _process_snapshot(pid: int) -> dict[str, float]:
    """CPU, I/O, memory and handle counters for a running process."""
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessIoCounters.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoCounters),
    ]
    kernel32.GetProcessIoCounters.restype = wintypes.BOOL
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION|VM_READ
    if not handle:
        return {}
    try:
        snap: dict[str, float] = {}
        creation = FileTime()
        exit_time = FileTime()
        kernel = FileTime()
        user = FileTime()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):

            def _filetime_ms(value: Any) -> float:
                ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
                return ticks / 10_000.0

            snap["cpu_kernel_ms"] = _filetime_ms(kernel)
            snap["cpu_user_ms"] = _filetime_ms(user)
            snap["cpu_total_ms"] = _filetime_ms(kernel) + _filetime_ms(user)
        counters = IoCounters()
        if kernel32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            snap["read_operations"] = float(counters.ReadOperationCount)
            snap["read_bytes"] = float(counters.ReadTransferCount)
            snap["write_bytes"] = float(counters.WriteTransferCount)
            snap["other_operations"] = float(counters.OtherOperationCount)
        memory = MemoryCounters()
        memory.cb = ctypes.sizeof(MemoryCounters)
        if psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(memory), ctypes.sizeof(memory)
        ):
            snap["working_set_bytes"] = float(memory.WorkingSetSize)
            snap["peak_working_set_bytes"] = float(memory.PeakWorkingSetSize)
            snap["private_bytes"] = float(memory.PrivateUsage)
        handles = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(handles)):
            snap["handle_count"] = float(handles.value)
        return snap
    finally:
        kernel32.CloseHandle(handle)


def _list_modules(pid: int) -> list[str]:
    """Basenames of the DLL/extension modules currently mapped in ``pid``."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD

    handle = kernel32.OpenProcess(0x0410, False, pid)
    if not handle:
        return []
    try:
        modules = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(
            handle,
            modules,
            ctypes.sizeof(modules),
            ctypes.byref(needed),
            0x03,
        ):
            return []
        count = min(needed.value // ctypes.sizeof(wintypes.HMODULE), len(modules))
        names: list[str] = []
        buffer = ctypes.create_unicode_buffer(1024)
        for index in range(count):
            if not psapi.GetModuleFileNameExW(
                handle, modules[index], buffer, len(buffer)
            ):
                continue
            names.append(Path(buffer.value).name.lower())
        return names
    finally:
        kernel32.CloseHandle(handle)


def _frozen_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["NOVEL_HOME"] = str(home)
    env["NOVEL_MUSIC_HOME"] = str(home / "music")
    # A frozen app ignores PYTHONPATH, but remove it so a stray variable
    # cannot change the behaviour of the measurement itself.
    env.pop("PYTHONPATH", None)
    return env


def _log_offset(home: Path) -> int:
    log_path = home / "logs" / "novel-desktop.log"
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def _log_first_line_ms(home: Path, offset: int, started_wall: datetime) -> float | None:
    """Milliseconds from launch to the first log record written by the app."""
    log_path = home / "logs" / "novel-desktop.log"
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in data.splitlines():
        match = _LOG_TIMESTAMP_RE.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            continue
        return (stamp - started_wall).total_seconds() * 1000.0
    return None


def _window_run(
    command: list[str],
    home: Path,
    *,
    timeout: float = 30.0,
    track_modules: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch a desktop client once and time it to the visible window."""
    probe = _WindowProbe(DESKTOP_WINDOW_TITLE)
    started_wall = datetime.now()
    log_offset = _log_offset(home)
    started = time.perf_counter()
    proc = subprocess.Popen(  # noqa: S603 - launching our own artifact
        command,
        cwd=str(home),
        env=env or _frozen_env(home),
        stderr=subprocess.DEVNULL,
    )
    hit: dict[str, Any] | None = None
    window_ready_ms: float | None = None
    module_seen: dict[str, float] = {}
    next_module_poll = 0.0
    snapshot: dict[str, float] = {}
    while True:
        elapsed_ms = _elapsed(started)
        if proc.poll() is not None:
            break
        if elapsed_ms > timeout * 1000.0:
            break
        hits = probe.poll()
        if hits:
            hit = hits[0]
            window_ready_ms = elapsed_ms
            snapshot = _process_snapshot(int(hit["pid"]))
            if track_modules:
                for name in _list_modules(int(hit["pid"])):
                    module_seen.setdefault(name, elapsed_ms)
            break
        if track_modules and elapsed_ms >= next_module_poll:
            for name in _list_modules(proc.pid):
                module_seen.setdefault(name, elapsed_ms)
            next_module_poll = elapsed_ms + 25.0
        time.sleep(0.005)

    hits = probe.poll() if hit is None else [hit]
    if hits:
        for candidate in hits:
            probe.close(candidate)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    log_ms = _log_first_line_ms(home, log_offset, started_wall)
    result: dict[str, Any] = {
        "window_ready_ms": window_ready_ms,
        "log_first_line_ms": log_ms,
        "window_after_log_ms": (
            window_ready_ms - log_ms
            if window_ready_ms is not None and log_ms is not None
            else None
        ),
        "exit_code": proc.returncode,
        "window": hit,
        "snapshot": snapshot,
    }
    if window_ready_ms is None:
        result["error"] = f"window not visible within {timeout:.0f}s"
    if track_modules:
        result["module_first_seen_ms"] = module_seen
        result["module_count"] = len(module_seen)
    return result


def _source_gui_run(home: Path, *, timeout: float) -> dict[str, Any]:
    """Run the source desktop with the real pywebview backend."""
    env = _frozen_env(home)
    env["PYTHONPATH"] = str(SRC)
    return _window_run(
        [sys.executable, "-c", SOURCE_DESKTOP_CODE],
        home,
        timeout=timeout,
        env=env,
    )


def _find_pyspy() -> Path | None:
    candidate = Path(sys.executable).with_name("py-spy.exe")
    if candidate.is_file():
        return candidate
    found = shutil.which("py-spy")
    return Path(found) if found else None


def _summarize_speedscope(path: Path) -> dict[str, Any]:
    """Aggregate a py-spy speedscope profile by leaf frame and module file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    frames = data.get("shared", {}).get("frames", [])
    profiles = data.get("profiles", [])
    if not frames or not profiles:
        return {"available": False, "reason": "empty speedscope profile"}
    main = max(profiles, key=lambda profile: len(profile.get("samples") or []))
    samples = main.get("samples") or []
    leaves: Counter[tuple[str, str, Any]] = Counter()
    files: Counter[str] = Counter()
    deferred: set[str] = set()
    needles = ("aiohttp", "plugins/rule", "plugins\\rule", "task_manager")
    for stack in samples:
        if not stack:
            continue
        leaf = frames[stack[-1]]
        leaves[
            (
                str(leaf.get("name")),
                str(leaf.get("file")),
                leaf.get("line"),
            )
        ] += 1
        files[str(leaf.get("file")).replace("\\", "/")] += 1
        for index in stack:
            frame = frames[index]
            combined = f"{frame.get('name')} {frame.get('file')}"
            for needle in needles:
                if needle in combined:
                    deferred.add(needle)
    total = len(samples)
    return {
        "available": True,
        "main_thread": main.get("name"),
        "main_samples": total,
        "deferred_frames_seen": sorted(deferred),
        "top_leaf": [
            {
                "name": name,
                "file": file,
                "line": line,
                "samples": count,
                "pct": round(count * 100.0 / total, 1),
            }
            for (name, file, line), count in leaves.most_common(15)
        ],
        "top_files": [
            {"file": file, "samples": count, "pct": round(count * 100.0 / total, 1)}
            for file, count in files.most_common(12)
        ],
        "other_threads": [
            {
                "thread": profile.get("name"),
                "samples": len(profile.get("samples") or []),
            }
            for profile in profiles
            if profile is not main
        ],
    }


def _pyspy_profile(exe: Path, home: Path, timeout: float) -> dict[str, Any]:
    """Sample the frozen start-up with py-spy, stopping at the window."""
    pyspy = _find_pyspy()
    if pyspy is None:
        return {"available": False, "reason": "py-spy not installed"}
    out = home / "startup.speedscope.json"
    probe = _WindowProbe(DESKTOP_WINDOW_TITLE)
    started = time.perf_counter()
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(  # noqa: S603 - launching our own tooling
        [
            str(pyspy),
            "record",
            "-o",
            str(out),
            "--format",
            "speedscope",
            "--rate",
            "200",
            "--nonblocking",
            "-d",
            f"{max(timeout, 10.0):.0f}",
            "--",
            str(exe),
        ],
        cwd=str(home),
        env=_frozen_env(home),
        creationflags=creation_flags,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    hit: dict[str, Any] | None = None
    hits: list[dict[str, Any]] = []
    window_ready_ms: float | None = None
    while time.perf_counter() - started <= timeout:
        hits = probe.poll()
        if hits:
            hit = hits[0]
            window_ready_ms = _elapsed(started)
            break
        time.sleep(0.01)
    if hit is not None:
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except (AttributeError, OSError):
            proc.terminate()
    try:
        _stdout, stderr = proc.communicate(timeout=max(timeout, 20.0))
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()
    if hits:
        for candidate in hits:
            probe.close(candidate)
        # py-spy leaves the sampled application running when it stops.
        subprocess.run(  # noqa: S603 - cleaning up our own launch
            ["taskkill", "/PID", str(int(hit["pid"])), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    if not out.is_file():
        return {
            "available": False,
            "reason": "py-spy produced no profile",
            "stderr_tail": (stderr or "")[-400:],
        }
    summary = _summarize_speedscope(out)
    summary["window_ready_ms_under_sampling"] = window_ready_ms
    summary["stderr_tail"] = (stderr or "")[-400:]
    return summary


def _pyz_import_costs(
    pyz: Path, wanted: set[str], prefixes: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Time PyInstaller's per-module PYZ decompress + unmarshal for start-up."""
    try:
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except Exception as exc:  # noqa: BLE001 - tooling is optional
        return {"available": False, "reason": f"PyInstaller unavailable: {exc}"}
    if not pyz.is_file():
        return {"available": False, "reason": f"PYZ missing: {pyz}"}
    reader = ZlibArchiveReader(str(pyz))
    names = set(wanted)
    if prefixes:
        names.update(name for name in reader.toc if name.startswith(prefixes))
    costs: dict[str, float] = {}
    for name in sorted(names):
        if name not in reader.toc:
            continue
        start = time.perf_counter()
        try:
            reader.extract(name)
        except Exception:  # noqa: BLE001 - a broken entry is not the point
            continue
        costs[name] = (time.perf_counter() - start) * 1000.0
    top = sorted(costs.items(), key=lambda item: item[1], reverse=True)[:20]
    return {
        "available": True,
        "module_count": len(costs),
        "total_ms": round(sum(costs.values()), 2),
        "top": [{"module": name, "ms": round(value, 3)} for name, value in top],
    }


def _stats(values: list[float | None]) -> dict[str, float] | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return {
        "median": round(statistics.median(clean), 1),
        "min": round(min(clean), 1),
        "max": round(max(clean), 1),
        "stdev": round(statistics.pstdev(clean), 1),
    }


def _time_once(
    command: list[str], *, env: dict[str, str] | None = None
) -> tuple[float, int]:
    """Run one short command and return ``(elapsed_ms, exit_code)``."""
    start = time.perf_counter()
    proc = subprocess.run(  # noqa: S603 - fixed command
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return (time.perf_counter() - start) * 1000.0, proc.returncode


def _command_stats(runs: list[float], codes: list[int]) -> dict[str, Any]:
    return {
        "runs_ms": [round(value, 1) for value in runs],
        "exit_codes": codes,
        "median_ms": round(statistics.median(runs), 1),
        "min_ms": round(min(runs), 1),
        "max_ms": round(max(runs), 1),
        "stdev_ms": round(statistics.pstdev(runs), 1),
    }


def _module_medians(runs: list[dict[str, Any]]) -> dict[str, float]:
    timelines = [
        run["module_first_seen_ms"] for run in runs if run.get("module_first_seen_ms")
    ]
    if not timelines:
        return {}
    common = set.intersection(*(set(timeline) for timeline in timelines))
    return {
        name: round(statistics.median(timeline[name] for timeline in timelines), 1)
        for name in common
    }


def _key_module_times(
    module_medians: dict[str, float], limit: int = 12
) -> list[dict[str, Any]]:
    boot = {
        "python312.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "python3.dll",
    }
    interesting = {
        "python312.dll",
        "_socket.pyd",
        "_ssl.pyd",
        "_hashlib.pyd",
        "_ctypes.pyd",
        "_sqlite3.pyd",
        "_wmi.pyd",
        "_asyncio.pyd",
        "pydantic_core",
        "webview2loader.dll",
        "microsoft.web.webview2.core.dll",
        "python.runtime.dll",
        "clr.dll",
        "coreclr.dll",
        "mscoree.dll",
        "clrjit.dll",
    }
    selected: dict[str, float] = {}
    for name, value in module_medians.items():
        if name in boot or any(token in name for token in interesting):
            selected[name] = value
    latest = sorted(module_medians.items(), key=lambda item: item[1])[-limit:]
    for name, value in latest:
        selected.setdefault(name, value)
    return [
        {"module": name, "ms": value}
        for name, value in sorted(selected.items(), key=lambda item: item[1])
    ]


def _print_frozen_summary(report: dict[str, Any]) -> None:
    comparison = report["comparison"]
    print("\n=== Frozen desktop (window-ready probe) ===")
    print(
        f"window ready : {comparison['frozen_window_ready_ms']:.1f} ms "
        f"(min {comparison['frozen_min_ms']:.1f}, "
        f"max {comparison['frozen_max_ms']:.1f})"
    )
    print(
        f"source stub  : {comparison['source_window_ready_ms']:.1f} ms "
        f"(min {comparison['source_min_ms']:.1f}, "
        f"max {comparison['source_max_ms']:.1f}; Python path, no GUI)"
    )
    if comparison.get("source_gui_window_ready_ms") is not None:
        print(
            f"source GUI   : {comparison['source_gui_window_ready_ms']:.1f} ms "
            f"(real pywebview/WebView2)"
        )
        print(
            f"delta vs GUI : {comparison['frozen_vs_source_gui_delta_ms']:+.1f} ms "
            f"({comparison['frozen_vs_source_gui_ratio']:.2f}x)"
        )
    print(
        f"delta vs stub: {comparison['delta_ms']:+.1f} ms ({comparison['ratio']:.2f}x)"
    )
    phases = report["frozen_desktop"]["phase_medians"]
    print(f"bootloader   : {phases.get('bootloader_ms', 0.0):.1f} ms")
    print(f"app imports  : {phases.get('app_imports_ms', 0.0):.1f} ms")
    print(f"GUI / WebView: {phases.get('gui_ms', 0.0):.1f} ms")
    print(
        f"CPU          : {phases.get('cpu_total_ms', 0.0):.1f} ms "
        f"({phases.get('cpu_wall_pct', 0.0):.1f}% of wall)"
    )
    print(
        f"I/O read     : {phases.get('read_mib', 0.0):.1f} MiB, "
        f"working set {phases.get('working_set_mib', 0.0):.1f} MiB"
    )
    print("\n=== Frozen CLI --version ===")
    cli = report["frozen_cli"]
    print(
        f"frozen {cli['frozen']['median_ms']:.1f} ms vs "
        f"source {cli['source']['median_ms']:.1f} ms "
        f"({cli['overhead_ms']:+.1f} ms)"
    )
    pyz = report["pyz_import_costs"]
    if pyz.get("available"):
        print(
            f"\n=== PYZ import cost ({pyz['module_count']} modules, "
            f"{pyz['total_ms']:.1f} ms) ==="
        )
        for row in pyz["top"][:10]:
            print(f"{row['ms']:>8.2f} ms  {row['module']}")
    pyspy = report["py_spy"]
    if pyspy.get("available"):
        print(f"\n=== py-spy main thread ({pyspy['main_samples']} samples) ===")
        for row in pyspy["top_leaf"][:10]:
            print(f"{row['pct']:>5.1f}%  {row['name']} [{row['file']}:{row['line']}]")
        print(
            "deferred frames seen:",
            ", ".join(pyspy["deferred_frames_seen"]) or "none",
        )
    else:
        print(f"\npy-spy: {pyspy.get('reason', 'unavailable')}")


def _frozen_report(
    *,
    exe: Path,
    cli: Path,
    repeat: int,
    out_path: Path,
    timeout: float,
    run_pyspy: bool,
) -> int:
    """Measure the frozen desktop, compare it with source and write JSON."""
    if sys.platform != "win32":
        raise SystemExit("--frozen needs Windows (Win32 window probe)")

    package_version = _package_version()
    exe_version = _exe_version(exe)
    if not exe.is_file():
        raise SystemExit(f"frozen desktop executable missing: {exe}")
    if not _versions_match(exe_version, package_version):
        raise SystemExit(
            f"refusing to profile {exe}: version {exe_version} does not match "
            f"source {package_version}; rebuild with scripts/build_desktop.py"
        )

    repeats = max(1, repeat)

    print(f"Frozen bundle : {exe}")
    print(f"Version       : {exe_version} (source {package_version})")
    print(f"Source runs   : {repeats}")

    source_runs: list[dict[str, Any]] = []
    timed_runs: list[dict[str, Any]] = []
    source_gui_runs: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(
            prefix="noveldl-frozen-startup-", ignore_cleanup_errors=True
        ) as frozen_temp,
        tempfile.TemporaryDirectory(
            prefix="noveldl-source-gui-", ignore_cleanup_errors=True
        ) as source_temp,
    ):
        home = Path(frozen_temp)
        source_home = Path(source_temp)
        for index in range(repeats):
            # Interleave source and frozen launches so a slow machine or an
            # antivirus scan cannot bias one side of the comparison.
            source_runs.append(_run_child("window"))
            if index % 2 == 0:
                source_gui_runs.append(_source_gui_run(source_home, timeout=timeout))
                timed_runs.append(_window_run([str(exe)], home, timeout=timeout))
            else:
                timed_runs.append(_window_run([str(exe)], home, timeout=timeout))
                source_gui_runs.append(_source_gui_run(source_home, timeout=timeout))
        diagnostic_runs = [
            _window_run([str(exe)], home, timeout=timeout, track_modules=True)
            for _ in range(min(3, repeats))
        ]

    source_ready = statistics.median(
        run["window_ready_total_ms"] for run in source_runs
    )
    source_import_rows = _importtime_rows()
    source_import_top = [
        {"module": name, "cumulative_ms": round(value / 1000.0, 1)}
        for value, name in sorted(source_import_rows)[-15:][::-1]
    ]
    source_modules = set(
        source_runs[0].get("window_modules", source_runs[0].get("modules", []))
    )
    deferred_before_window = {
        name: bool(source_runs[0].get("deferred_before_window", {}).get(name))
        for name in DEFERRED_MODULES
    }
    plugins_before_window = int(
        source_runs[0].get("registered_plugins_after_window", 0) or 0
    )
    source_gui_stats = _stats([run["window_ready_ms"] for run in source_gui_runs])

    failures = [run["error"] for run in timed_runs if run.get("error")]
    window_times = [run["window_ready_ms"] for run in timed_runs]
    window_stats = _stats(window_times)
    if window_stats is None or failures:
        raise SystemExit(
            "frozen desktop never showed its window: "
            + "; ".join(failures or ["no measurement"])
        )
    log_stats = _stats([run["log_first_line_ms"] for run in timed_runs])
    snapshot_stats: dict[str, dict[str, float] | None] = {}
    snapshot_keys = (
        "cpu_total_ms",
        "read_bytes",
        "write_bytes",
        "working_set_bytes",
        "handle_count",
    )
    for key in snapshot_keys:
        snapshot_stats[key] = _stats(
            [
                float(run["snapshot"][key])
                for run in timed_runs
                if key in run["snapshot"]
            ]
        )

    module_medians = _module_medians(diagnostic_runs)
    python_dll_ms = module_medians.get("python312.dll")
    log_ms = log_stats["median"] if log_stats else None
    phase_medians: dict[str, float] = {
        "window_ready_ms": window_stats["median"],
    }
    if python_dll_ms is not None:
        phase_medians["bootloader_ms"] = python_dll_ms
    if python_dll_ms is not None and log_ms is not None:
        phase_medians["app_imports_ms"] = round(log_ms - python_dll_ms, 1)
    if log_ms is not None:
        phase_medians["gui_ms"] = round(window_stats["median"] - log_ms, 1)
    cpu_stats = snapshot_stats.get("cpu_total_ms")
    if cpu_stats:
        phase_medians["cpu_total_ms"] = cpu_stats["median"]
        phase_medians["cpu_wall_pct"] = round(
            cpu_stats["median"] * 100.0 / window_stats["median"], 1
        )
    read_stats = snapshot_stats.get("read_bytes")
    if read_stats:
        phase_medians["read_mib"] = round(read_stats["median"] / (1024 * 1024), 1)
    working_set = snapshot_stats.get("working_set_bytes")
    if working_set:
        phase_medians["working_set_mib"] = round(
            working_set["median"] / (1024 * 1024), 1
        )

    with tempfile.TemporaryDirectory(
        prefix="noveldl-frozen-cli-", ignore_cleanup_errors=True
    ) as temp:
        home = Path(temp)
        frozen_env = _frozen_env(home)
        source_env = _frozen_env(home)
        source_env["PYTHONPATH"] = str(SRC)
        frozen_cmd = [str(cli), "--version"]
        source_cmd = [sys.executable, "-c", SOURCE_CLI_CODE, "--version"]
        frozen_ms: list[float] = []
        frozen_codes: list[int] = []
        source_ms: list[float] = []
        source_codes: list[int] = []
        for index in range(repeats):
            pairs = [
                ("source", source_cmd, source_env),
                ("frozen", frozen_cmd, frozen_env),
            ]
            if index % 2:
                pairs.reverse()
            for label, command, env in pairs:
                elapsed, code = _time_once(command, env=env)
                if label == "source":
                    source_ms.append(elapsed)
                    source_codes.append(code)
                else:
                    frozen_ms.append(elapsed)
                    frozen_codes.append(code)
        frozen_cli = _command_stats(frozen_ms, frozen_codes)
        source_cli = _command_stats(source_ms, source_codes)

    pyz = _pyz_import_costs(
        FROZEN_PYZ,
        source_modules,
        prefixes=("webview", "clr", "pythonnet", "cffi"),
    )

    pyspy: dict[str, Any] = {"available": False, "reason": "skipped"}
    if run_pyspy:
        with tempfile.TemporaryDirectory(
            prefix="noveldl-frozen-pyspy-", ignore_cleanup_errors=True
        ) as temp:
            pyspy = _pyspy_profile(exe, Path(temp), timeout)

    bundle_bytes = sum(
        path.stat().st_size for path in exe.parent.rglob("*") if path.is_file()
    )
    bundle_files = sum(1 for path in exe.parent.rglob("*") if path.is_file())
    pyz_modules = None
    if FROZEN_PYZ.is_file():
        try:
            from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader

            pyz_modules = len(ZlibArchiveReader(str(FROZEN_PYZ)).toc)
        except Exception:  # noqa: BLE001 - informational only
            pyz_modules = None

    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": package_version,
        "frozen_exe": str(exe),
        "frozen_exe_version": exe_version,
        "repeat": repeats,
        "bundle": {
            "files": bundle_files,
            "bytes": bundle_bytes,
            "pyz_bytes": FROZEN_PYZ.stat().st_size if FROZEN_PYZ.is_file() else None,
            "pyz_modules": pyz_modules,
        },
        "comparison": {
            "source_window_ready_ms": round(source_ready, 1),
            "source_min_ms": round(
                min(run["window_ready_total_ms"] for run in source_runs), 1
            ),
            "source_max_ms": round(
                max(run["window_ready_total_ms"] for run in source_runs), 1
            ),
            "frozen_window_ready_ms": window_stats["median"],
            "frozen_min_ms": window_stats["min"],
            "frozen_max_ms": window_stats["max"],
            "frozen_stdev_ms": window_stats["stdev"],
            "delta_ms": round(window_stats["median"] - source_ready, 1),
            "ratio": round(window_stats["median"] / source_ready, 2),
            "prior_source_reference_ms": 106.0,
            "source_gui_window_ready_ms": source_gui_stats["median"]
            if source_gui_stats
            else None,
            "frozen_vs_source_gui_delta_ms": round(
                window_stats["median"] - source_gui_stats["median"], 1
            )
            if source_gui_stats
            else None,
            "frozen_vs_source_gui_ratio": round(
                window_stats["median"] / source_gui_stats["median"], 2
            )
            if source_gui_stats
            else None,
        },
        "source_real_gui": {
            "window_ready_ms": source_gui_stats,
            "runs": [
                {key: value for key, value in run.items() if key != "snapshot"}
                for run in source_gui_runs
            ],
            "failures": [run["error"] for run in source_gui_runs if run.get("error")],
        },
        "source": {
            "window_ready_total_ms": round(source_ready, 1),
            "deferred_before_window": deferred_before_window,
            "plugins_registered_before_window": plugins_before_window,
            "import_top": source_import_top,
            "import_total_ms": round(
                max((value for value, _name in source_import_rows), default=0) / 1000.0,
                1,
            ),
        },
        "frozen_desktop": {
            "runs": [
                {key: value for key, value in run.items() if key != "snapshot"}
                | {"snapshot": run.get("snapshot", {})}
                for run in timed_runs
            ],
            "phase_medians": phase_medians,
            "log_first_line_ms": log_stats,
            "cpu": snapshot_stats.get("cpu_total_ms"),
            "read": snapshot_stats.get("read_bytes"),
            "working_set": snapshot_stats.get("working_set_bytes"),
            "handles": snapshot_stats.get("handle_count"),
            "module_first_seen_ms": module_medians,
            "key_modules": _key_module_times(module_medians),
            "diagnostic_windows_ms": [
                round(run["window_ready_ms"], 1)
                for run in diagnostic_runs
                if run.get("window_ready_ms") is not None
            ],
            "diagnostic_runs": [
                {
                    key: value
                    for key, value in run.items()
                    if key != "module_first_seen_ms"
                }
                for run in diagnostic_runs
            ],
        },
        "frozen_cli": {
            "frozen": frozen_cli,
            "source": source_cli,
            "overhead_ms": round(frozen_cli["median_ms"] - source_cli["median_ms"], 1),
        },
        "pyz_import_costs": pyz,
        "py_spy": pyspy,
        "notes": [
            "onedir bundle: no per-launch extraction to a temp directory; "
            "the PYZ archive is decompressed module-by-module on import.",
            "Runs are new processes but the Windows file cache is warm; the "
            "session has no admin rights, so the standby list cannot be "
            "cleared for a true cold-disk baseline.",
            "Antivirus/EDR latency is not directly observable; read bytes, "
            "process CPU and the CPU/wall ratio bound its contribution.",
            "The desktop executable is windowed and has no --version; the "
            "visible pywebview window is the ready signal. CLI --version "
            "isolates PyInstaller boot + interpreter start-up.",
            "The --repeat source baseline stubs pywebview, so it measures the "
            "Python import path only. source_real_gui launches the real "
            "backend and is the like-for-like comparison for the frozen "
            "window-ready number.",
            "Source and frozen launches are interleaved to share the same "
            "machine load; each reported number is still a median of the "
            "requested cold processes.",
            "Module first-seen times come from up to three extra diagnostic "
            "launches with module polling (~25 ms resolution), reported as "
            "their median; timed runs do not poll.",
            "py-spy sampling slows the measured launch and is reported "
            "separately, never mixed into the timed median.",
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _print_frozen_summary(report)
    print(f"\nWrote {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--child",
        choices=("phases", "window"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="cold processes per mode (median is reported; default 3)",
    )
    parser.add_argument("--json-out", type=Path, help="write the medians as JSON")
    parser.add_argument(
        "--compare",
        type=Path,
        help="baseline JSON written by a previous --json-out run",
    )
    parser.add_argument(
        "--importtime",
        action="store_true",
        help="also list the slowest cumulative imports",
    )
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="measure the PyInstaller desktop bundle instead of the source tree",
    )
    parser.add_argument(
        "--frozen-exe",
        type=Path,
        default=FROZEN_DESKTOP_EXE,
        help=f"desktop bundle to profile (default: {FROZEN_DESKTOP_EXE})",
    )
    parser.add_argument(
        "--frozen-cli",
        type=Path,
        default=FROZEN_CLI_EXE,
        help=f"frozen CLI used for --version (default: {FROZEN_CLI_EXE})",
    )
    parser.add_argument(
        "--frozen-out",
        type=Path,
        default=FROZEN_REPORT_PATH,
        help=f"JSON report path (default: {FROZEN_REPORT_PATH})",
    )
    parser.add_argument(
        "--frozen-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for the window (default: 30)",
    )
    parser.add_argument(
        "--no-pyspy",
        action="store_true",
        help="skip the optional py-spy sampling pass",
    )
    args = parser.parse_args(argv)

    if args.child:
        return _child_main([args.child])

    if args.frozen:
        return _frozen_report(
            exe=args.frozen_exe,
            cli=args.frozen_cli,
            repeat=args.repeat,
            out_path=args.frozen_out,
            timeout=args.frozen_timeout,
            run_pyspy=not args.no_pyspy,
        )

    repeats = max(1, args.repeat)
    phase_runs = [_run_child("phases") for _ in range(repeats)]
    window_runs = [_run_child("window") for _ in range(repeats)]

    phases = _median_phases(phase_runs)
    window = _median_phases(window_runs)
    deferred = sorted(
        {
            name
            for run in phase_runs
            for name, loaded in run["deferred_imported"].items()
            if loaded
        }
    )
    plugins = max(int(run["registered_plugins_after_window"]) for run in phase_runs)

    baseline: dict[str, float] | None = None
    if args.compare:
        saved = json.loads(args.compare.read_text(encoding="utf-8"))
        baseline = {key: float(value) for key, value in saved["phases"].items()}

    _print_table("Detailed phases (stubbed window)", phases, baseline)
    window_order = (
        "import_main",
        "prepare_home",
        "setup_logging",
        "import_webview",
        "window_ready",
        "services_done",
    )
    _print_table("Real entry point (fresh process)", window, None, window_order)
    ready_total = statistics.median(run["window_ready_total_ms"] for run in window_runs)
    services_total = statistics.median(
        run["services_done_total_ms"] for run in window_runs
    )
    print(f"\nWindow ready after {ready_total:.1f} ms (imports included)")
    print(f"Queue restore finished after {services_total:.1f} ms")
    print("\nWindow is ready before these imports:", deferred or "none")
    print("Plugin searchers registered before the window:", plugins)

    payload = {
        "phases": phases,
        "window": window,
        "window_ready_total_ms": ready_total,
        "services_done_total_ms": services_total,
        "deferred_imported": deferred,
        "registered_plugins_after_window": plugins,
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json_out}")

    if args.importtime:
        _importtime_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
