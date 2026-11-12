#!/usr/bin/env python3
"""Read-only smoke test for the frozen Windows bundles.

PyInstaller reports success even when a bundle is missing an optional runtime
dependency, because every such import is lazy.  The failure then only shows up
on a user's machine, at download time, in the image-mode site parsers.  This
script inspects and (safely) launches the produced executables instead:

* every requested launcher must exist -- a missing bundle is a hard failure;
* the launcher's Windows ``ProductVersion`` must equal
  ``novel_downloader.__version__`` (the same value the installer and the
  update manifest use);
* the image stack (``numpy``, ``Pillow`` and ``fontTools``) must be present in
  the PYZ archive, together with Pillow's native ``_imagingft`` module;
* the size-hygiene contract holds: dev/test modules and hook-collected payload
  that ``packaging/_spec_common.py`` prunes must not reappear, while the emoji
  runtime table the music adapters use must still be frozen;
* ``novel-cli --version`` must print the package version and
  ``novel-cli doctor --json`` must emit parseable JSON;
* ``novel-api --help`` must exit 0 (it imports the whole ASGI stack).

The desktop executable is windowed (``console=False``); it is never launched
here, only its launcher, version resource and payload are inspected.

Usage (Windows runner, after ``scripts/build_cli.py`` / ``build_desktop.py``)::

    python scripts/verify_frozen.py
    python scripts/verify_frozen.py --bundles cli,api
    python scripts/verify_frozen.py --bundles desktop --root .
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: ``(key, dist directory, launcher, PyInstaller work directory)``.  The work
#: directory is where the PYZ archive records the frozen modules.
BUNDLES: dict[str, tuple[str, str, str]] = {
    "cli": ("dist/novel-cli", "novel-cli.exe", "build/novel_cli"),
    "api": ("dist/novel-api", "novel-api.exe", "build/novel_api"),
    "desktop": ("dist/NovelDownloader", "NovelDownloader.exe", "build/novel_desktop"),
}

#: Modules the image-mode site parsers and the yuewen font decoder import.
#: ``packaging/_spec_common.py`` lists the same names as hidden imports; the
#: test suite keeps the two lists identical.
IMAGE_STACK_MODULES: tuple[str, ...] = (
    "numpy",
    "numpy.typing",
    "PIL.Image",
    "PIL.ImageOps",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "fontTools",
    "fontTools.ttLib",
    "fontTools.ttLib.tables._p_o_s_t",
)

#: Modules every spec deliberately keeps out of the bundles.  A stale spec or
#: a PyInstaller hook update silently re-adds them (``setuptools`` alone is
#: ~1.8 MiB of bytecode plus its runtime hook), so the gate checks absence.
PRUNED_MODULES: tuple[str, ...] = (
    "setuptools",
    "_distutils_hack",
    "numpy.testing",
    "numpy.f2py",
    "numpy.random",
)

#: Hook-collected trees removed by ``packaging/_spec_common.prune_bundle_toc``.
#: Only the per-language emoji tables go: ``import emoji`` opens the English
#: base table ``emoji.json`` at load time, so that file must survive.
#: ``numpy.random`` is excluded outright, extension modules included.
PRUNED_DATA_GLOBS: tuple[str, ...] = (
    "emoji/unicode_codes/emoji_*.json",
    "numpy/random/*.pyd",
)

#: Data files pruning must never remove (opened by ``import emoji``).
REQUIRED_DATA_FILES: tuple[str, ...] = ("emoji/unicode_codes/emoji.json",)

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


class VerifyError(RuntimeError):
    """A bundle is missing or unusable; the message is safe to print."""


@dataclass
class BundleReport:
    """Collected verdicts for one bundle."""

    key: str
    exe: Path
    checks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.problems


def read_package_version(root: Path) -> str:
    """Return ``__version__`` from the source tree (never imports the package)."""
    init_py = root / "src" / "novel_downloader" / "__init__.py"
    if not init_py.is_file():
        raise VerifyError(f"package version file not found: {init_py}")
    match = _VERSION_RE.search(init_py.read_text(encoding="utf-8"))
    if match is None:
        raise VerifyError(f"no __version__ string in {init_py}")
    return match.group(1).strip()


def _expected_image_modules(root: Path) -> tuple[str, ...]:
    """Read the image hidden-import list from ``packaging/_spec_common.py``."""
    path = root / "packaging" / "_spec_common.py"
    if not path.is_file():
        return IMAGE_STACK_MODULES
    try:
        spec = importlib.util.spec_from_file_location("_verify_spec_common", path)
        if spec is None or spec.loader is None:
            return IMAGE_STACK_MODULES
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        declared = getattr(module, "IMAGE_STACK_HIDDEN_IMPORTS", None)
        if isinstance(declared, tuple):
            return tuple(str(name) for name in declared)
    except Exception:  # noqa: BLE001 - the local constant is the fallback
        pass
    return IMAGE_STACK_MODULES


def _run(
    command: list[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a probe with a timeout, capturing UTF-8 text output."""
    # The frozen app writes Chinese localisation strings; without this the
    # pipe uses the Windows ANSI code page and JSON parsing dies on the first
    # non-ASCII byte.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )


def _pyz_modules(work_dir: Path) -> set[str] | None:
    """Frozen module names from the build's PYZ, or None when unavailable."""
    archives = sorted(work_dir.glob("PYZ-*.pyz"))
    if not archives:
        return None
    try:
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except ImportError:
        return None
    try:
        return set(ZlibArchiveReader(str(archives[0])).toc)
    except Exception:  # noqa: BLE001 - a corrupt archive is reported by callers
        return None


def _windows_product_version(exe: Path) -> tuple[str | None, str | None]:
    """
    Read the ``ProductVersion`` string of *exe*.

    :return: ``(version, error)``; a non-Windows host returns ``(None, None)``.
    """
    if os.name != "nt":
        return None, None
    literal = str(exe).replace("'", "''")
    script = f"(Get-Item -LiteralPath '{literal}').VersionInfo.ProductVersion"
    try:
        proc = _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            cwd=exe.parent,
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot read the version resource: {exc}"
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        return None, f"cannot read the version resource: {proc.stderr.strip()}"
    return value, None


def _check_launcher(report: BundleReport) -> None:
    if not report.exe.is_file():
        report.problems.append(f"missing launcher: {report.exe}")
        return
    report.checks.append(f"launcher present ({report.exe.stat().st_size} bytes)")


def _check_version_resource(report: BundleReport, expected: str) -> None:
    if not report.exe.is_file():
        return
    version, error = _windows_product_version(report.exe)
    if error is not None:
        report.problems.append(error)
        return
    if version is None:
        report.checks.append("version resource: skipped (non-Windows host)")
        return
    if version != expected:
        report.problems.append(f"ProductVersion is {version!r}, expected {expected!r}")
        return
    report.checks.append(f"ProductVersion {version}")


def _check_image_payload(
    report: BundleReport, root: Path, dist_dir: Path, work_dir: Path
) -> None:
    internal = dist_dir / "_internal"
    native = sorted(internal.glob("PIL/_imagingft*.pyd"))
    if not native:
        report.problems.append(
            f"missing Pillow FreeType module under {internal / 'PIL'}"
        )
    else:
        report.checks.append(f"Pillow native module {native[0].name}")

    numpy_dirs = sorted(path.name for path in internal.glob("numpy*") if path.is_dir())
    if not numpy_dirs:
        report.problems.append(f"missing numpy payload under {internal}")
    else:
        report.checks.append(f"numpy payload {', '.join(numpy_dirs[:2])}")

    modules = _pyz_modules(work_dir)
    if modules is None:
        report.problems.append(
            f"cannot read the PYZ archive under {work_dir} "
            "(build it first, or install PyInstaller)"
        )
        return
    expected_modules = _expected_image_modules(root)
    missing = [name for name in expected_modules if name not in modules]
    if missing:
        report.problems.append("PYZ is missing image modules: " + ", ".join(missing))
    else:
        report.checks.append(
            f"image modules in PYZ ({len(expected_modules)} module names)"
        )


def _check_bundle_hygiene(report: BundleReport, dist_dir: Path, work_dir: Path) -> None:
    """Fail when pruned dev/test payload crept back into the bundle."""
    internal = dist_dir / "_internal"
    leftovers = sorted(
        path.relative_to(internal).as_posix()
        for pattern in PRUNED_DATA_GLOBS
        for path in internal.glob(pattern)
    )
    if leftovers:
        shown = ", ".join(leftovers[:5])
        more = "" if len(leftovers) <= 5 else f" (+{len(leftovers) - 5} more)"
        report.problems.append(f"bundle still ships pruned payload: {shown}{more}")

    missing = [
        relative
        for relative in REQUIRED_DATA_FILES
        if not (internal / relative).is_file()
    ]
    if missing and internal.is_dir():
        report.problems.append("bundle is missing required data: " + ", ".join(missing))

    modules = _pyz_modules(work_dir)
    if modules is None:
        # _check_image_payload already reported the unreadable archive.
        return
    present = [name for name in PRUNED_MODULES if name in modules]
    if present:
        joined = ", ".join(present)
        report.problems.append(f"PYZ still contains pruned modules: {joined}")
        return
    if "emoji.unicode_codes.data_dict" not in modules:
        report.problems.append("PYZ is missing emoji.unicode_codes.data_dict")
        return
    report.checks.append(
        "size hygiene: dev/test payload pruned, emoji runtime table present"
    )


def _check_cli_probes(report: BundleReport, expected: str, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="noveldl-verify-") as work:
        cwd = Path(work)
        try:
            version_proc = _run(
                [str(report.exe), "--version"], cwd=cwd, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.problems.append(f"--version could not run: {exc}")
            return
        if version_proc.returncode != 0:
            report.problems.append(
                f"--version exited {version_proc.returncode}: "
                f"{version_proc.stderr.strip()}"
            )
        elif expected not in version_proc.stdout:
            report.problems.append(
                f"--version printed {version_proc.stdout.strip()!r}, "
                f"expected to contain {expected!r}"
            )
        else:
            report.checks.append(f"--version -> {version_proc.stdout.strip()}")

        try:
            doctor_proc = _run(
                [str(report.exe), "doctor", "--json"], cwd=cwd, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.problems.append(f"doctor --json could not run: {exc}")
            return
        if doctor_proc.returncode != 0:
            report.problems.append(
                f"doctor --json exited {doctor_proc.returncode}: "
                f"{doctor_proc.stderr.strip()}"
            )
            return
        try:
            document = json.loads(doctor_proc.stdout)
        except ValueError as exc:
            report.problems.append(f"doctor --json is not JSON: {exc}")
            return
        if not isinstance(document, dict):
            report.problems.append("doctor --json returned a non-object")
            return
        checks = document.get("checks")
        summary = document.get("summary")
        if not isinstance(checks, list) or not checks:
            report.problems.append("doctor --json has no checks")
            return
        if not isinstance(summary, dict):
            report.problems.append("doctor --json has no summary object")
            return
        report.checks.append(
            f"doctor --json parsed ({len(checks)} checks, ok={summary.get('ok')!r})"
        )


def _check_api_probe(report: BundleReport, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="noveldl-verify-") as work:
        try:
            proc = _run([str(report.exe), "--help"], cwd=Path(work), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.problems.append(f"--help could not run: {exc}")
            return
    if proc.returncode != 0:
        report.problems.append(
            f"--help exited {proc.returncode}: {proc.stderr.strip()}"
        )
        return
    if "--port" not in proc.stdout:
        report.problems.append("--help output does not look like the API parser")
        return
    report.checks.append("--help imported the API stack")


def verify_bundle(
    key: str,
    root: Path,
    expected_version: str,
    *,
    timeout: float,
) -> BundleReport:
    """Inspect one bundle and return its report (never raises for a failure)."""
    dist_rel, exe_name, work_rel = BUNDLES[key]
    dist_dir = root / dist_rel
    work_dir = root / work_rel
    report = BundleReport(key=key, exe=dist_dir / exe_name)

    _check_launcher(report)
    _check_version_resource(report, expected_version)
    _check_image_payload(report, root, dist_dir, work_dir)
    _check_bundle_hygiene(report, dist_dir, work_dir)
    if key == "cli":
        if report.exe.is_file():
            _check_cli_probes(report, expected_version, timeout)
    elif key == "api":
        if report.exe.is_file():
            _check_api_probe(report, timeout)
    else:
        # Windowed GUI: launching it would block on WebView2, so only the
        # launcher, resource and payload are inspected.
        report.checks.append("desktop GUI not launched (windowed bundle)")
    return report


def _parse_bundles(value: str) -> list[str]:
    keys: list[str] = []
    for raw in value.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in BUNDLES:
            raise argparse.ArgumentTypeError(
                f"unknown bundle {key!r}; choose from {', '.join(BUNDLES)}"
            )
        if key not in keys:
            keys.append(key)
    if not keys:
        raise argparse.ArgumentTypeError("at least one bundle is required")
    return keys


def main(argv: list[str] | None = None) -> int:
    """Verify the requested bundles and return a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bundles",
        default="cli,api,desktop",
        type=_parse_bundles,
        help="comma-separated bundles to verify (default: all three)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="seconds allowed per launched probe (default: 180)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    try:
        expected = read_package_version(root)
    except VerifyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Repository : {root}")
    print(f"Version    : {expected}")
    reports = [
        verify_bundle(key, root, expected, timeout=args.timeout) for key in args.bundles
    ]

    failed = 0
    print()
    for report in reports:
        status = "OK  " if report.ok() else "FAIL"
        print(f"[{status}] {report.key}: {report.exe}")
        for check in report.checks:
            print(f"         - {check}")
        for problem in report.problems:
            print(f"         ! {problem}")
        if not report.ok():
            failed += 1

    print()
    if failed:
        print(f"{failed} bundle(s) failed verification.")
        return 1
    print("All requested bundles passed verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
