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

Usage::

    .venv/Scripts/python.exe scripts/profile_desktop_startup.py
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --json-out before.json
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --compare before.json
    .venv/Scripts/python.exe scripts/profile_desktop_startup.py --importtime
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
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


def _importtime_report() -> None:
    """Show the slowest cumulative imports for the window path."""
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
    print("\nSlowest cumulative imports (us)")
    for cumulative, name in sorted(rows)[-15:]:
        print(f"{cumulative / 1000.0:>10.1f} ms  {name}")
    total = max((value for value, _name in rows), default=0) / 1000.0
    print(f"\nTOTAL (root import) {total:.1f} ms")


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
    args = parser.parse_args(argv)

    if args.child:
        return _child_main([args.child])

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
