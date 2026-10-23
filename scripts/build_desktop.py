#!/usr/bin/env python3
"""
Build the Windows desktop application with PyInstaller.

Usage::

    .venv/Scripts/python.exe scripts/build_desktop.py
    .venv/Scripts/python.exe scripts/build_desktop.py --no-clean
    .venv/Scripts/python.exe scripts/build_desktop.py --clean-dist

The spec lives in ``packaging/novel_desktop.spec``; this wrapper only checks
the toolchain, cleans stale output, invokes PyInstaller and reports the result.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib.util import find_spec
from pathlib import Path

APP_NAME = "NovelDownloader"
SPEC_RELPATH = Path("packaging") / "novel_desktop.spec"
DIST_RELPATH = Path("dist") / APP_NAME
WORK_RELPATH = Path("build") / "novel_desktop"

# Files/directories that must exist after a successful build. Checked relative
# to the onedir bundle; PyInstaller 6 keeps collected files under _internal.
SANITY_CHECKS = (
    Path("_internal") / "novel_downloader" / "resources" / "icons" / "app.ico",
    Path("_internal")
    / "novel_downloader"
    / "locales"
    / "zh_CN"
    / "LC_MESSAGES"
    / "messages.mo",
    Path("_internal")
    / "novel_downloader"
    / "plugins"
    / "rule"
    / "builtin"
    / "biquge5r.json",
    Path("_internal") / "webview" / "lib" / "Microsoft.Web.WebView2.Core.dll",
    Path("_internal") / "nicegui" / "static",
)


def _repo_root() -> Path:
    """Repository root (the parent of ``scripts/``)."""
    return Path(__file__).resolve().parents[1]


def _dir_size(path: Path) -> int:
    """Total size of a directory tree in bytes."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _human_size(num_bytes: int) -> str:
    """Format a byte count as MiB (decimal-friendly for build reports)."""
    return f"{num_bytes / (1024 * 1024):.1f} MiB"


def _check_toolchain() -> bool:
    """Verify PyInstaller and the desktop runtime are importable."""
    ok = True

    if find_spec("PyInstaller") is None:
        print(
            "ERROR: PyInstaller is not installed in this interpreter.",
            file=sys.stderr,
        )
        print(f"  {sys.executable} -m pip install pyinstaller", file=sys.stderr)
        ok = False

    for module, extra in (("nicegui", "desktop"), ("webview", "desktop")):
        if find_spec(module) is None:
            print(f"ERROR: missing runtime dependency '{module}'.", file=sys.stderr)
            print(
                f'  {sys.executable} -m pip install -e ".[{extra}]"',
                file=sys.stderr,
            )
            ok = False

    if sys.platform != "win32":
        print(
            "WARNING: this spec targets Windows; the produced bundle will not "
            "run on this platform.",
            file=sys.stderr,
        )

    return ok


def _clean(work: Path, dist: Path, clean_dist: bool) -> None:
    """Remove stale build output so no removed module lingers in the bundle."""
    if work.exists():
        print(f"Cleaning work directory: {work}")
        shutil.rmtree(work, ignore_errors=True)
    if clean_dist and dist.exists():
        print(f"Cleaning previous bundle: {dist}")
        shutil.rmtree(dist, ignore_errors=True)


def _run_pyinstaller(spec: Path, root: Path) -> int:
    """Invoke PyInstaller in-process and return an exit code."""
    import PyInstaller.__main__

    args = [
        str(spec),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build"),
    ]
    print("Running:", " ".join([sys.executable, "-m", "PyInstaller", *args]))
    try:
        PyInstaller.__main__.run(args)
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return 0
        print(f"PyInstaller exited with status {code!r}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - unexpected PyInstaller failure
        print(f"PyInstaller failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _report(dist: Path) -> int:
    """Print artifact information and verify the expected payload exists."""
    exe = dist / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"ERROR: build finished but {exe} is missing.", file=sys.stderr)
        return 3

    print()
    print("Build succeeded.")
    print(f"  Executable : {exe}")
    print(f"  Bundle dir : {dist}")
    print(f"  Exe size   : {_human_size(exe.stat().st_size)}")
    print(f"  Total size : {_human_size(_dir_size(dist))}")

    missing = [str(item) for item in SANITY_CHECKS if not (dist / item).exists()]
    if missing:
        print("WARNING: some expected bundled resources are missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        return 3

    print("  Bundled resources: OK")
    print()
    print("Run it with:")
    print(f'  "{exe}"')
    print("Logs/state live under %LOCALAPPDATA%\\novel_downloader by default.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Build the desktop app and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Keep the previous build/novel_desktop work directory.",
    )
    parser.add_argument(
        "--clean-dist",
        action="store_true",
        help="Also remove the previous dist/NovelDownloader bundle first.",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    spec = root / SPEC_RELPATH
    dist = root / DIST_RELPATH
    work = root / WORK_RELPATH

    if not spec.is_file():
        print(f"ERROR: spec not found: {spec}", file=sys.stderr)
        return 2

    print(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    print(f"Repository : {root}")

    if not _check_toolchain():
        return 1

    if not args.no_clean:
        _clean(work, dist, args.clean_dist)

    code = _run_pyinstaller(spec, root)
    if code != 0:
        print("Build FAILED; see the PyInstaller output above.", file=sys.stderr)
        return code

    return _report(dist)


if __name__ == "__main__":
    sys.exit(main())
