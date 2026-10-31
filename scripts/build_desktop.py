#!/usr/bin/env python3
"""
Build the standalone ``NovelDownloader`` desktop bundle with PyInstaller.

Usage::

    .venv/Scripts/python.exe scripts/build_desktop.py
    .venv/Scripts/python.exe scripts/build_desktop.py --clean-dist
    .venv/Scripts/python.exe scripts/build_desktop.py --clean-dist --smoke

The spec lives in ``packaging/novel_desktop.spec``.  This wrapper checks the
toolchain, optionally removes stale output, invokes PyInstaller and then
verifies that the bundle really is a windowed onedir build carrying the runtime
data files (locales, builtin rules, resources), the dynamically imported site
plugins and the pywebview runtime (WebView2 loader binaries) the window needs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path

APP_NAME = "NovelDownloader"
SPEC_REL = "packaging/novel_desktop.spec"
WORK_NAME = "novel_desktop"

# Modules that must be importable in the build environment.  (module, purpose)
CORE_MODULES: tuple[tuple[str, str], ...] = (
    ("webview", "desktop window (pywebview, renders via Edge WebView2)"),
    ("rich", "logging/CLI rendering"),
    ("requests", "HTTP client"),
    ("aiohttp", "async HTTP client"),
    ("lxml", "HTML parsing"),
    ("cssselect", "HTML parsing"),
    ("platformdirs", "user data/config paths"),
    ("Crypto", "pycryptodome"),
)
# The music tab is optional; warn instead of aborting when these are absent.
MUSIC_MODULES: tuple[tuple[str, str], ...] = (
    ("mutagen", "audio metadata"),
    ("pathvalidate", "file names"),
    ("PIL", "cover art"),
    ("qrcode", "login QR codes"),
    ("websocket", "websocket-client"),
)

MIN_SITE_SEARCHERS = 57

#: Sanity window for the unpacked bundle.  A bundle far below the lower bound
#: means resources were dropped; far above it means something heavy slipped in.
MIN_BUNDLE_BYTES = 40 * 1024 * 1024
MAX_BUNDLE_BYTES = 900 * 1024 * 1024

#: PE optional-header subsystem values.
_IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
_IMAGE_SUBSYSTEM_WINDOWS_CUI = 3


@dataclass
class BuildResult:
    """Outcome of the desktop build, used for the final summary."""

    ok: bool = False
    message: str = ""
    stage: str = "validate"
    exe: Path | None = None
    exe_size: int = 0
    total_size: int = 0
    site_modules: int = 0
    site_searchers: int = 0
    site_dirs: int = 0
    checks: list[tuple[str, bool]] = field(default_factory=list)

    def check(self, label: str, passed: bool) -> bool:
        """Record one named verification step."""
        self.checks.append((label, passed))
        return passed


def _repo_root() -> Path:
    """Repository root (the parent of ``scripts/``)."""
    return Path(__file__).resolve().parents[1]


def _human_size(num_bytes: int) -> str:
    """Format a byte count as MiB (or GiB once it gets large)."""
    mib = num_bytes / (1024 * 1024)
    if mib >= 1024:
        return f"{mib / 1024:.2f} GiB"
    return f"{mib:.1f} MiB"


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
        print(f"{level}: missing module '{module}' ({purpose}).", file=sys.stderr)
        if fatal:
            print(
                f"  {sys.executable} -m pip install {module}",
                file=sys.stderr,
            )
            ok = False
    return ok


def _check_toolchain() -> bool:
    """Verify PyInstaller and the desktop runtime dependencies."""
    ok = True
    if find_spec("PyInstaller") is None:
        print(
            "ERROR: PyInstaller is not installed in this interpreter.",
            file=sys.stderr,
        )
        print(f"  {sys.executable} -m pip install pyinstaller", file=sys.stderr)
        ok = False

    ok = _check_modules(CORE_MODULES, fatal=True) and ok
    _check_modules(MUSIC_MODULES, fatal=False)
    return ok


def _clean(paths: list[Path]) -> None:
    """Remove stale build/dist output for the desktop target."""
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


def _has_webview_modules(internal: Path, modules: list[str]) -> bool:
    """
    Whether the frozen bundle carries the pywebview package and backends.

    Depending on the PyInstaller version modules end up in the PYZ archive,
    in ``base_library.zip`` or as a directory next to the executable, so all
    three places are accepted.
    """
    needed = ("webview", "webview.platforms.edgechromium")
    if all(name in modules for name in needed):
        return True
    webview_dir = internal / "webview"
    if webview_dir.is_dir() and (webview_dir / "platforms").is_dir():
        return True
    base_library = internal / "base_library.zip"
    if base_library.is_file():
        import zipfile

        with zipfile.ZipFile(base_library) as archive:
            names = archive.namelist()
            return all(
                any(name == f"{need.replace('.', '/')}.pyc" for name in names)
                for need in needed
            )
    return False


def _pe_subsystem(exe: Path) -> int | None:
    """
    Read the Windows PE optional-header subsystem of ``exe``.

    :return: ``2`` for a GUI application, ``3`` for a console one, or ``None``
        when the header cannot be parsed (non-Windows build, truncated file).
    """
    try:
        with exe.open("rb") as handle:
            header = handle.read(4096)
    except OSError:
        return None
    if len(header) < 0x40 or header[:2] != b"MZ":
        return None
    (pe_offset,) = struct.unpack_from("<I", header, 0x3C)
    subsystem_offset = pe_offset + 4 + 20 + 68  # sig + COFF header + subsystem
    if subsystem_offset + 2 > len(header):
        return None
    if header[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    (subsystem,) = struct.unpack_from("<H", header, subsystem_offset)
    return int(subsystem)


def _smoke_launch(exe: Path, seconds: float) -> tuple[bool, str]:
    """
    Start the windowed executable for a few seconds and confirm it survives.

    A GUI bundle cannot be driven without a user, so the only thing this can
    assert is that start-up does not crash immediately.  ``NOVEL_HOME`` points
    at a throw-away directory so the real user profile is never touched.
    """
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="novel-desktop-smoke-") as home:
        env["NOVEL_HOME"] = home
        try:
            proc = subprocess.Popen(  # noqa: S603 - launching our own artifact
                [str(exe)],
                cwd=home,
                env=env,
            )
        except OSError as exc:
            return False, f"could not start the executable: {exc}"

        try:
            time.sleep(seconds)
            code = proc.poll()
            if code is None:
                return True, f"still running after {seconds:.0f}s, terminated cleanly"
            if code == 0:
                return True, f"exited with code 0 before {seconds:.0f}s"
            return False, f"exited early with code {code}"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
                    proc.wait(timeout=10)


def _validate(root: Path, *, smoke_seconds: float) -> BuildResult:
    """Verify the produced bundle and return its measurements."""
    dist_dir = root / "dist" / APP_NAME
    work_dir = root / "build" / WORK_NAME
    result = BuildResult()

    exe = dist_dir / f"{APP_NAME}.exe"
    if not exe.is_file():
        exe = dist_dir / APP_NAME  # non-Windows fallback for diagnostics
    if not exe.is_file():
        result.message = f"executable missing: {exe}"
        return result

    result.exe = exe
    result.exe_size = exe.stat().st_size
    result.total_size = _dir_size(dist_dir)

    # PyInstaller 6 keeps everything except the launcher under _internal/.
    internal = dist_dir / "_internal"
    package = internal / "novel_downloader"
    result.check("executable present", True)
    result.check(
        "locales messages.mo",
        (package / "locales" / "zh_CN" / "LC_MESSAGES" / "messages.mo").is_file(),
    )
    result.check(
        "locales messages.po",
        (package / "locales" / "zh_CN" / "LC_MESSAGES" / "messages.po").is_file(),
    )
    result.check(
        "resources icon",
        (package / "resources" / "icons" / "app.ico").is_file(),
    )
    result.check("package marker py.typed", (package / "py.typed").is_file())
    result.check(
        "musicdl SOURCE.txt",
        (
            package
            / "apps"
            / "music"
            / "providers"
            / "_vendor"
            / "musicdl"
            / "SOURCE.txt"
        ).is_file(),
    )

    builtin_dir = package / "plugins" / "rule" / "builtin"
    result.check(
        "builtin rule JSON shipped",
        builtin_dir.is_dir() and any(builtin_dir.glob("*.json")),
    )

    result.site_dirs = _count_site_dirs(package / "plugins" / "sites")
    result.check(
        f"site directories >= {MIN_SITE_SEARCHERS} ({result.site_dirs})",
        result.site_dirs >= MIN_SITE_SEARCHERS,
    )

    # pywebview: the WebView2 loader binaries collected by the community
    # ``hook-webview``, plus the webview Python modules (which may live in
    # the PYZ archive or as a package directory).  Their presence is what
    # proves the window can actually be created in the frozen app.
    loader_dll = internal / "WebView2Loader.dll"
    result.check(
        "WebView2 loader binaries",
        loader_dll.is_file() or any(internal.glob("**/WebView2Loader*.dll")),
    )

    modules = _read_pyz_modules(work_dir)
    result.check("webview Python modules", _has_webview_modules(internal, modules))
    result.site_modules = sum(
        1 for mod in modules if mod.startswith("novel_downloader.plugins.sites.")
    )
    result.site_searchers = sum(
        1
        for mod in modules
        if mod.startswith("novel_downloader.plugins.sites.")
        and mod.endswith(".searcher")
    )
    result.check("PYZ archive found", bool(modules))
    result.check(
        f"site searchers >= {MIN_SITE_SEARCHERS} ({result.site_searchers})",
        result.site_searchers >= MIN_SITE_SEARCHERS,
    )

    if sys.platform == "win32":
        subsystem = _pe_subsystem(exe)
        result.check(
            "windowed PE subsystem (no console)",
            subsystem == _IMAGE_SUBSYSTEM_WINDOWS_GUI,
        )

    if not MIN_BUNDLE_BYTES <= result.total_size <= MAX_BUNDLE_BYTES:
        print(
            f"WARNING: bundle size {_human_size(result.total_size)} is outside the "
            f"expected {_human_size(MIN_BUNDLE_BYTES)}.."
            f"{_human_size(MAX_BUNDLE_BYTES)} range.",
            file=sys.stderr,
        )

    if smoke_seconds > 0:
        alive, detail = _smoke_launch(exe, smoke_seconds)
        result.check(f"start-up smoke test ({detail})", alive)

    failed = [label for label, passed in result.checks if not passed]
    result.ok = not failed
    result.message = "; ".join(failed)
    return result


def _print_report(result: BuildResult) -> None:
    """Print a human-readable report for the desktop bundle."""
    print()
    print(f"=== {APP_NAME} ===")
    if result.exe is not None:
        print(f"  Executable  : {result.exe}")
        print(f"  Exe size    : {_human_size(result.exe_size)}")
        print(f"  Bundle size : {_human_size(result.total_size)}")
        print(f"  Site modules bundled : {result.site_modules}")
        print(f"  Site searchers       : {result.site_searchers}")
        print(f"  Site directories     : {result.site_dirs}")
    print("  Checks:")
    for label, passed in result.checks:
        print(f"    [{'ok' if passed else 'FAIL'}] {label}")
    if result.ok and not result.checks:
        print("    (not run)")
    print(f"  Result : {'OK' if result.ok else 'FAIL - ' + result.message}")


def build(
    *,
    clean_dist: bool = False,
    smoke_seconds: float = 0.0,
    skip_toolchain: bool = False,
) -> BuildResult:
    """
    Build and validate the desktop bundle without printing the summary.

    This is the single entry point used both by :func:`main` and by
    ``build_release.py``.

    :param clean_dist: Also remove ``dist/NovelDownloader`` before building.
    :param smoke_seconds: Launch the executable for this many seconds after a
        successful validation (0 disables the smoke test).
    :param skip_toolchain: Skip the build-environment dependency check.
    :return: A :class:`BuildResult` whose ``ok`` flag reports success.
    """
    root = _repo_root()
    spec = root / SPEC_REL
    if not spec.is_file():
        return BuildResult(
            stage="prepare",
            message=f"spec not found: {spec}",
        )
    if not skip_toolchain and not _check_toolchain():
        return BuildResult(stage="prepare", message="toolchain check failed")

    dist_dir = root / "dist" / APP_NAME
    work_dir = root / "build" / WORK_NAME
    _clean([work_dir, dist_dir] if clean_dist else [work_dir])

    code = _run_pyinstaller(spec, root)
    if code != 0:
        return BuildResult(
            stage="pyinstaller",
            message=f"PyInstaller exited with status {code}",
        )
    return _validate(root, smoke_seconds=smoke_seconds)


def main(argv: list[str] | None = None) -> int:
    """Build the desktop bundle and return a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--clean-dist",
        action="store_true",
        help="Remove dist/NovelDownloader and build/novel_desktop before building.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Launch the built executable for a few seconds to check start-up.",
    )
    parser.add_argument(
        "--smoke-seconds",
        type=float,
        default=8.0,
        help="How long the smoke launch should stay up (default: 8).",
    )
    parser.add_argument(
        "--skip-toolchain",
        action="store_true",
        help="Skip the build-environment dependency check.",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    print(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    print(f"Repository : {root}")

    result = build(
        clean_dist=args.clean_dist,
        smoke_seconds=args.smoke_seconds if args.smoke else 0.0,
        skip_toolchain=args.skip_toolchain,
    )
    _print_report(result)
    if not result.ok:
        print()
        print(f"Build FAILED for {APP_NAME}: {result.message}")
        return {"prepare": 2, "pyinstaller": 3}.get(result.stage, 4)

    print()
    print(f"{APP_NAME} built successfully.")
    if result.exe is not None:
        print(f'  Run it with: "{result.exe}"')
    installer = _maybe_build_installer(root)
    if installer is not None:
        print(f'  Installer  : "{installer}"')
    return 0


def _find_iscc() -> str | None:
    """Locate the Inno Setup compiler, or ``None`` when absent."""
    from shutil import which

    found = which("iscc")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _maybe_build_installer(root: Path) -> Path | None:
    """
    Compile the Inno Setup installer when the toolchain is available.

    A missing ISCC is a warning, not an error: the zip bundle stays the
    baseline artifact and the installer is an optional extra.
    """
    version = "unknown"
    try:
        from novel_downloader import __version__

        version = str(__version__)
    except ImportError:
        pass
    iscc = _find_iscc()
    if iscc is None:
        print(
            "  Installer  : skipped (Inno Setup 6 not installed; "
            "install it or run iscc manually, see packaging/noveldl_installer.iss)",
        )
        return None
    cmd = [
        iscc,
        f"/DAppVersion={version}",
        str(root / "packaging" / "noveldl_installer.iss"),
    ]
    print("  Compiling installer:", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=root, check=False)  # noqa: S603 - fixed args
    if proc.returncode != 0:
        print("  Installer compilation FAILED", file=sys.stderr)
        return None
    out = root / "dist" / f"NovelDownloader-setup-{version}-x64.exe"
    return out if out.is_file() else None


if __name__ == "__main__":
    raise SystemExit(main())
