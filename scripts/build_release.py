#!/usr/bin/env python3
"""
Build release artifacts for novel-downloader (CLI, API and desktop bundles).

Usage::

    .venv/Scripts/python.exe scripts/build_release.py
    .venv/Scripts/python.exe scripts/build_release.py --targets cli,api
    .venv/Scripts/python.exe scripts/build_release.py \\
        --targets cli,api,desktop --out dist/release

Every requested target is built with the same logic as ``build_cli.py`` /
``build_desktop.py`` (including their validation steps), archived as a zip and
collected in::

    <out>/novel-downloader-<version>-<platform>/
        novel-cli-<version>-<platform>.zip
        novel-api-<version>-<platform>.zip
        NovelDownloader-<version>-<platform>.zip
        SHA256SUMS.txt
        manifest.json

``SHA256SUMS.txt`` uses the standard ``<sha256>  <filename>`` format and can be
verified with ``sha256sum -c`` (or ``Get-FileHash`` on Windows).  The script
exits non-zero if any requested target fails to build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for _path in (str(SRC_DIR), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import build_cli  # noqa: E402
import build_desktop  # noqa: E402

from novel_downloader import __version__  # noqa: E402

PROJECT_NAME = "novel-downloader"
DEFAULT_OUT = Path("dist") / "release"
CHUNK_SIZE = 1024 * 1024


class BuildOutcome(Protocol):
    """Minimal result interface shared by the two build wrappers."""

    ok: bool
    message: str


@dataclass(frozen=True)
class TargetSpec:
    """One releasable bundle and the function that builds it."""

    key: str
    label: str
    bundle: str  # directory name under dist/
    build: Callable[[], BuildOutcome]


TARGETS: dict[str, TargetSpec] = {
    "cli": TargetSpec(
        key="cli",
        label="novel-cli",
        bundle="novel-cli",
        build=lambda: build_cli.build("cli"),
    ),
    "api": TargetSpec(
        key="api",
        label="novel-api",
        bundle="novel-api",
        build=lambda: build_cli.build("api"),
    ),
    "desktop": TargetSpec(
        key="desktop",
        label=build_desktop.APP_NAME,
        bundle=build_desktop.APP_NAME,
        # A release build must never inherit stale files from an earlier
        # onedir layout, so the desktop dist directory is always removed.
        build=lambda: build_desktop.build(clean_dist=True),
    ),
}


def _parse_targets(value: str) -> list[str]:
    """Parse a comma-separated target list, preserving its order."""
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not items or items == ["all"]:
        return list(TARGETS)
    unknown = [item for item in items if item not in TARGETS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown target(s): {', '.join(unknown)} "
            f"(choose from {', '.join(TARGETS)})"
        )
    return list(dict.fromkeys(items))


def _platform_tag() -> str:
    """Return a release-friendly ``<os>-<arch>`` tag (e.g. ``windows-x64``)."""
    system = platform.system().lower()
    os_name = {"windows": "windows", "darwin": "macos"}.get(system, system or "unknown")
    machine = platform.machine().lower()
    arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86": "x86",
        "i386": "x86",
        "i686": "x86",
    }.get(machine, machine or "unknown")
    return f"{os_name}-{arch}"


def _human_size(num_bytes: int) -> str:
    """Format a byte count as KiB/MiB/GiB."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - unreachable


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_stats(path: Path) -> tuple[int, int]:
    """Return ``(total_bytes, file_count)`` for an onedir bundle."""
    total = 0
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return total, count


def _archive_bundle(bundle_dir: Path, archive_path: Path) -> None:
    """Zip ``bundle_dir`` so extracting yields a same-named top-level folder."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_stem = archive_path.with_suffix("")
    produced = shutil.make_archive(
        str(archive_stem),
        "zip",
        root_dir=str(bundle_dir.parent),
        base_dir=bundle_dir.name,
    )
    produced_path = Path(produced)
    if produced_path != archive_path:  # pragma: no cover - defensive
        produced_path.replace(archive_path)


def _timestamp() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_artifact(
    spec: TargetSpec,
    release_dir: Path,
    version: str,
    platform_tag: str,
) -> dict[str, Any]:
    """Build, archive and measure one target; raise on build failure."""
    started = time.monotonic()
    outcome = spec.build()
    if not outcome.ok:
        raise RuntimeError(outcome.message or "build failed")

    bundle_dir = REPO_ROOT / "dist" / spec.bundle
    if not bundle_dir.is_dir():
        raise RuntimeError(f"bundle directory missing: {bundle_dir}")

    archive_name = f"{spec.bundle}-{version}-{platform_tag}.zip"
    archive_path = release_dir / archive_name
    bundle_size, file_count = _bundle_stats(bundle_dir)
    _archive_bundle(bundle_dir, archive_path)

    elapsed = time.monotonic() - started
    return {
        "target": spec.key,
        "name": spec.label,
        "file": archive_name,
        "size": archive_path.stat().st_size,
        "sha256": _sha256_file(archive_path),
        "bundle_size": bundle_size,
        "file_count": file_count,
        "built_at": _timestamp(),
        "build_seconds": round(elapsed, 1),
    }


def _write_checksums(release_dir: Path, artifacts: Sequence[dict[str, Any]]) -> Path:
    """Write ``SHA256SUMS.txt`` in the conventional two-space format."""
    lines = [f"{item['sha256']}  {item['file']}" for item in artifacts]
    path = release_dir / "SHA256SUMS.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def _write_manifest(
    release_dir: Path,
    *,
    version: str,
    platform_tag: str,
    requested: Sequence[str],
    artifacts: Sequence[dict[str, Any]],
    failures: Sequence[dict[str, str]],
    created_at: str,
) -> Path:
    """Write ``manifest.json`` describing the release contents."""
    manifest = {
        "name": PROJECT_NAME,
        "version": version,
        "platform": platform_tag,
        "created_at": created_at,
        "python": platform.python_version(),
        "targets": list(requested),
        "artifacts": list(artifacts),
        "failures": list(failures),
    }
    path = release_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _print_table(artifacts: Sequence[dict[str, Any]]) -> None:
    """Print a compact summary table of the produced archives."""
    headers = ("Target", "Artifact", "Size", "SHA256")
    rows = [
        (
            item["target"],
            item["file"],
            _human_size(item["size"]),
            item["sha256"][:12],
        )
        for item in artifacts
    ]
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rows))
        for column in range(len(headers))
    ]

    def render(row: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(width) for value, width in zip(row, widths, strict=True)
        ).rstrip()

    print()
    print(render(headers))
    print(render(tuple("-" * width for width in widths)))
    for row in rows:
        print(render(row))


def main(argv: list[str] | None = None) -> int:
    """Build the requested targets and return a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--targets",
        type=_parse_targets,
        default=list(TARGETS),
        help=(
            "Comma-separated targets to build "
            f"(default: {','.join(TARGETS)}; 'all' selects every target)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output root, relative to the repository (default: {DEFAULT_OUT}).",
    )
    args = parser.parse_args(argv)

    targets: list[str] = args.targets
    out_root: Path = args.out
    if not out_root.is_absolute():
        out_root = REPO_ROOT / out_root

    version = __version__
    platform_tag = _platform_tag()
    release_dir = out_root / f"{PROJECT_NAME}-{version}-{platform_tag}"

    print(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    print(f"Repository : {REPO_ROOT}")
    print(f"Version    : {version}")
    print(f"Platform   : {platform_tag}")
    print(f"Targets    : {', '.join(targets)}")
    print(f"Release dir: {release_dir}")

    if release_dir.exists():
        print(f"Cleaning   : {release_dir}")
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for key in targets:
        spec = TARGETS[key]
        print()
        print(f"### Building {spec.label} ({key})")
        try:
            artifact = _build_artifact(spec, release_dir, version, platform_tag)
        except (RuntimeError, OSError) as exc:
            failures.append({"target": key, "message": str(exc)})
            print(f"FAILED: {spec.label}: {exc}", file=sys.stderr)
            continue
        artifacts.append(artifact)
        print(
            f"  -> {artifact['file']} "
            f"({_human_size(artifact['size'])}, "
            f"{artifact['file_count']} files, {artifact['build_seconds']}s)"
        )

    created_at = _timestamp()
    checksums = _write_checksums(release_dir, artifacts)
    manifest = _write_manifest(
        release_dir,
        version=version,
        platform_tag=platform_tag,
        requested=targets,
        artifacts=artifacts,
        failures=failures,
        created_at=created_at,
    )

    _print_table(artifacts)
    print()
    print(f"Checksums : {checksums}")
    print(f"Manifest  : {manifest}")
    print(f"Release   : {release_dir}")

    if failures:
        print()
        print(
            "Release build FAILED for: "
            + ", ".join(item["target"] for item in failures)
        )
        return 1

    print()
    print("Release build completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
