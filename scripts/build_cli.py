#!/usr/bin/env python3
"""
Build the standalone ``novel-cli`` and/or ``novel-api`` bundles with PyInstaller.

Usage::

    .venv/Scripts/python.exe scripts/build_cli.py
    .venv/Scripts/python.exe scripts/build_cli.py --target cli
    .venv/Scripts/python.exe scripts/build_cli.py --target api

The specs live in ``packaging/``.  This wrapper only checks the toolchain,
removes stale output, invokes PyInstaller and verifies that the bundle contains
the runtime data files and the dynamically loaded plugin modules that static
analysis cannot see.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

TARGETS: dict[str, tuple[str, str]] = {
    "cli": ("novel-cli", "packaging/novel_cli.spec"),
    "api": ("novel-api", "packaging/novel_api.spec"),
}

# Modules that must be importable in the build environment.  (module, purpose)
CORE_MODULES: tuple[tuple[str, str], ...] = (
    ("rich", "CLI rendering"),
    ("requests", "HTTP client"),
    ("aiohttp", "async HTTP client"),
    ("lxml", "HTML parsing"),
    ("cssselect", "HTML parsing"),
    ("platformdirs", "user data/config paths"),
    ("Crypto", "pycryptodome"),
)
API_MODULES: tuple[tuple[str, str], ...] = (
    ("fastapi", "REST API"),
    ("uvicorn", "ASGI server"),
)
# Music support is optional; warn instead of aborting when it is absent.
MUSIC_MODULES: tuple[tuple[str, str], ...] = (
    ("mutagen", "audio metadata"),
    ("pathvalidate", "file names"),
    ("PIL", "cover art"),
    ("qrcode", "login QR codes"),
    ("websocket", "websocket-client"),
)

MIN_SITE_SEARCHERS = 57


@dataclass
class BuildResult:
    """Outcome of one target build, used for the final summary."""

    target: str
    name: str
    ok: bool = False
    message: str = ""
    exe: Path | None = None
    exe_size: int = 0
    total_size: int = 0
    site_searchers: int = 0
    site_modules: int = 0
    music_modules: int = 0


def _repo_root() -> Path:
    """Repository root (the parent of ``scripts/``)."""
    return Path(__file__).resolve().parents[1]


def _human_size(num_bytes: int) -> str:
    """Format a byte count as MiB."""
    return f"{num_bytes / (1024 * 1024):.1f} MiB"


def _dir_size(path: Path) -> int:
    """Total size of a directory tree in bytes."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _check_modules(modules: tuple[tuple[str, str], ...], *, fatal: bool) -> bool:
    """Report missing build-environment modules; return False if any are fatal."""
    ok = True
    for module, purpose in modules:
        if find_spec(module) is not None:
            continue
        level = "ERROR" if fatal else "WARNING"
        print(
            f"{level}: missing module '{module}' ({purpose}).",
            file=sys.stderr,
        )
        if fatal:
            print(
                f"  {sys.executable} -m pip install {module}",
                file=sys.stderr,
            )
            ok = False
    return ok


def _check_toolchain(target: str) -> bool:
    """Verify PyInstaller and the target's runtime dependencies."""
    ok = True
    if find_spec("PyInstaller") is None:
        print(
            "ERROR: PyInstaller is not installed in this interpreter.",
            file=sys.stderr,
        )
        print(f"  {sys.executable} -m pip install pyinstaller", file=sys.stderr)
        ok = False

    ok = _check_modules(CORE_MODULES, fatal=True) and ok
    if target == "api":
        ok = _check_modules(API_MODULES, fatal=True) and ok
    _check_modules(MUSIC_MODULES, fatal=False)
    return ok


def _clean(paths: list[Path]) -> None:
    """Remove stale build/dist output for this target."""
    for path in paths:
        if path.exists():
            print(f"Cleaning: {path}")
            shutil.rmtree(path, ignore_errors=True)


def _run_pyinstaller(spec: Path, root: Path) -> int:
    """Invoke PyInstaller in a subprocess and return its exit code."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(spec),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build"),
    ]
    print("Running:", " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=root, check=False).returncode
    except OSError as exc:  # pragma: no cover - unexpected interpreter failure
        print(f"ERROR: could not start PyInstaller: {exc}", file=sys.stderr)
        return 2


def _read_pyz_modules(work_dir: Path) -> list[str]:
    """Return the module names recorded in the build's PYZ archive."""
    pyz_files = sorted(work_dir.glob("PYZ-*.pyz"))
    if not pyz_files:
        return []
    from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader

    archive = ZlibArchiveReader(str(pyz_files[0]))
    return list(archive.toc)


def _count_site_dirs(sites_dir: Path) -> int:
    """Count site plugin directories reconstructed in the bundle."""
    if not sites_dir.is_dir():
        return 0
    return sum(1 for item in sites_dir.iterdir() if item.is_dir())


def _validate(target: str, name: str, root: Path) -> BuildResult:
    """Verify the produced bundle and return its measurements."""
    dist_dir = root / "dist" / name
    work_dir = root / "build" / Path(TARGETS[target][1]).stem
    result = BuildResult(target=target, name=name)

    exe = dist_dir / f"{name}.exe"
    if not exe.is_file():
        exe = dist_dir / name  # non-Windows fallback for diagnostics
    if not exe.is_file():
        result.message = f"executable missing: {exe}"
        return result

    result.exe = exe
    result.exe_size = exe.stat().st_size
    result.total_size = _dir_size(dist_dir)

    # PyInstaller 6 keeps everything except the launcher under _internal/.
    internal = dist_dir / "_internal"
    package = internal / "novel_downloader"
    required = {
        "locales messages.mo": package
        / "locales"
        / "zh_CN"
        / "LC_MESSAGES"
        / "messages.mo",
        "locales messages.po": package
        / "locales"
        / "zh_CN"
        / "LC_MESSAGES"
        / "messages.po",
        # Checked separately below so a renamed sample rule does not break the
        # build; the requirement is that at least one builtin rule ships.
        "resources icon": package / "resources" / "icons" / "app.ico",
        "package marker py.typed": package / "py.typed",
        "musicdl SOURCE.txt": package
        / "apps"
        / "music"
        / "providers"
        / "_vendor"
        / "musicdl"
        / "SOURCE.txt",
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    builtin_dir = package / "plugins" / "rule" / "builtin"
    if not builtin_dir.is_dir():
        missing.append("rule builtin directory missing")
    elif not any(builtin_dir.glob("*.json")):
        missing.append("rule builtin JSON (directory is empty)")

    modules = _read_pyz_modules(work_dir)
    result.site_modules = sum(
        1 for mod in modules if mod.startswith("novel_downloader.plugins.sites.")
    )
    result.site_searchers = sum(
        1
        for mod in modules
        if mod.startswith("novel_downloader.plugins.sites.")
        and mod.endswith(".searcher")
    )
    result.music_modules = sum(
        1
        for mod in modules
        if mod.startswith("novel_downloader.apps.music.providers._vendor.musicdl")
    )
    site_dirs = _count_site_dirs(package / "plugins" / "sites")

    problems = list(missing)
    if not modules:
        problems.append("PYZ archive not found in the build work directory")
    if result.site_searchers < MIN_SITE_SEARCHERS:
        problems.append(
            f"only {result.site_searchers} site searcher modules bundled "
            f"(expected >= {MIN_SITE_SEARCHERS})"
        )
    if site_dirs < MIN_SITE_SEARCHERS:
        problems.append(
            f"only {site_dirs} site directories on disk "
            f"(expected >= {MIN_SITE_SEARCHERS})"
        )

    result.ok = not problems
    result.message = "; ".join(problems)
    return result


def _print_report(result: BuildResult) -> None:
    """Print a human-readable per-target report."""
    print()
    print(f"=== {result.name} ===")
    if result.exe is not None:
        print(f"  Executable : {result.exe}")
        print(f"  Exe size   : {_human_size(result.exe_size)}")
        print(f"  Bundle size: {_human_size(result.total_size)}")
        print(f"  Site modules bundled : {result.site_modules}")
        print(f"  Site searchers       : {result.site_searchers}")
        print(f"  Musicdl modules      : {result.music_modules}")
    if result.ok:
        print("  Result     : OK")
    else:
        print(f"  Result     : FAIL - {result.message}")


def _parse_targets(value: str) -> list[str]:
    return ["cli", "api"] if value == "both" else [value]


def main(argv: list[str] | None = None) -> int:
    """Build the requested targets and return a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=("cli", "api", "both"),
        default="both",
        help="Which bundle to build (default: both).",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    print(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    print(f"Repository : {root}")

    results: list[BuildResult] = []
    for target in _parse_targets(args.target):
        name, spec_rel = TARGETS[target]
        spec = root / spec_rel
        print()
        print(f"### Building {name}")
        if not spec.is_file():
            results.append(
                BuildResult(
                    target=target,
                    name=name,
                    message=f"spec not found: {spec}",
                )
            )
            _print_report(results[-1])
            continue

        if not _check_toolchain(target):
            results.append(
                BuildResult(
                    target=target,
                    name=name,
                    message="toolchain check failed",
                )
            )
            _print_report(results[-1])
            continue

        dist_dir = root / "dist" / name
        work_dir = root / "build" / spec.stem
        _clean([work_dir, dist_dir])

        code = _run_pyinstaller(spec, root)
        if code != 0:
            results.append(
                BuildResult(
                    target=target,
                    name=name,
                    message=f"PyInstaller exited with status {code}",
                )
            )
            _print_report(results[-1])
            continue

        result = _validate(target, name, root)
        results.append(result)
        _print_report(result)

    failed = [result for result in results if not result.ok]
    print()
    if failed:
        print("Build FAILED for:", ", ".join(result.name for result in failed))
        return 3

    print("All requested targets built successfully.")
    print()
    print("Run with:")
    for result in results:
        if result.exe is not None:
            print(f'  "{result.exe}" --help')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
