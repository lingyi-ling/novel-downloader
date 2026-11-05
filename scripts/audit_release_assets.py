#!/usr/bin/env python3
"""Audit built release assets before they are uploaded.

The audit is offline and read-only.  It checks a staging directory (the
``dist/release/novel-downloader-<version>-<platform>`` layout produced by
``scripts/build_release.py``) for three classes of mistakes:

``publication surface``
    Every file must be on the release-name allowlist, be non-empty, have a
    plausible size and -- when ``--release-json`` is given -- match the asset
    set of the remote release exactly.

``archive contents``
    ZIP/APK members must not contain traversal paths, VCS/build directories,
    credentials, source maps or debug symbols.  Python files are allowed (a
    PyInstaller bundle may collect a few) and are only counted for review.

``raw bytes``
    ZIP/EXE/APK payloads are streamed in 1 MiB chunks and scanned for private
    key headers, deploy passwords and token-shaped strings.  Matched values
    are never printed: only the marker name and the file name appear in the
    report.

Usage::

    python scripts/audit_release_assets.py --dir dist/release/.../ 
    python scripts/audit_release_assets.py --dir dist/release/.../ --json
    python scripts/audit_release_assets.py \
        --release-json payload.json --dir dist/release/.../

Exit codes: ``0`` clean, ``1`` problems found, ``2`` usage/IO error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_ERROR = 2

MAX_ASSET_NAME = 200
MIN_ARCHIVE_SIZE = 1024
MAX_ASSET_SIZE = 300 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
RAW_CHUNK_SIZE = 1024 * 1024
RAW_OVERLAP = 96

ALLOWED_SUFFIXES = frozenset({".zip", ".exe", ".apk", ".txt", ".json"})
ARCHIVE_SUFFIXES = frozenset({".zip", ".apk"})
RAW_SCAN_SUFFIXES = frozenset({".zip", ".exe", ".apk"})

#: ``<version>`` as produced by ``novel_downloader.__version__``.
_VERSION = r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?"
#: ``<os>-<arch>`` as produced by ``build_release._platform_tag()``.
_PLATFORM = r"[A-Za-z0-9_]+-[A-Za-z0-9_]+"
#: ``<apk.stem>`` for multi-APK Android releases.
_APK_STEM = r"[A-Za-z0-9][A-Za-z0-9._-]*"

_ALLOWED_ASSET_RE = re.compile(
    rf"(?:"
    rf"novel-(?:cli|api)-{_VERSION}-{_PLATFORM}\.zip"
    rf"|NovelDownloader-{_VERSION}-{_PLATFORM}\.zip"
    rf"|NovelDownloader-setup-{_VERSION}-[A-Za-z0-9_]+\.exe"
    rf"|novel-downloader-{_VERSION}-android(?:-{_APK_STEM})?\.apk"
    rf"|SHA256SUMS\.txt"
    rf"|manifest\.json"
    rf"|latest\.json"
    rf")\Z"
)

FORBIDDEN_DIRECTORY_PARTS = frozenset(
    {".git", ".github", "__pycache__", ".pytest_cache", "node_modules"}
)
FORBIDDEN_FILE_NAMES = frozenset({".env"})
FORBIDDEN_FILE_PREFIXES = ("credentials", "id_rsa")
FORBIDDEN_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".jks",
    ".keystore",
    ".pdb",
    ".map",
    ".dsym",
)

#: certifi and Chaquopy ship public CA bundles named ``cacert.pem`` inside every
#: PyInstaller/Android payload; that is public data, not a credential.  Any
#: other ``*.pem`` file is still rejected.
SAFE_PEM_BASENAMES = frozenset({"cacert.pem"})

SECRET_MARKERS = (
    b"BEGIN OPENSSH PRIVATE KEY",
    b"BEGIN RSA PRIVATE KEY",
    b"github_pat_",
    b"ghp_",
    b"storePassword=",
    b"keyPassword=",
    b"NOVELDL_DEPLOY_PASSWORD",
)
TOKEN_PATTERN = re.compile(rb"gh[opsu]_[A-Za-z0-9_]{20,}")
TOKEN_LABEL = "GitHub token pattern (gh[opsu]_...)"

KIND_UNEXPECTED_NAME = "unexpected-name"
KIND_EMPTY_FILE = "empty-file"
KIND_SMALL_FILE = "suspiciously-small"
KIND_LARGE_FILE = "suspiciously-large"
KIND_UNSAFE_MEMBER = "unsafe-member-path"
KIND_FORBIDDEN_DIRECTORY = "forbidden-directory"
KIND_FORBIDDEN_FILE = "forbidden-file"
KIND_ARCHIVE_TOO_LARGE = "archive-too-large"
KIND_UNREADABLE_ARCHIVE = "unreadable-archive"
KIND_SECRET_MARKER = "secret-marker"
KIND_MISSING_ASSET = "missing-asset"
KIND_ORPHAN_FILE = "orphan-file"
KIND_UNSAFE_PATH = "unsafe-path"
KIND_UNEXPECTED_ENTRY = "unexpected-entry"
KIND_DUPLICATE_ASSET = "duplicate-asset"


class AuditError(Exception):
    """A usage or IO error that must abort the audit (exit code 2)."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One audit finding."""

    asset: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The outcome of auditing one release directory."""

    scanned: int
    problems: list[Problem]
    python_files: int

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_asset_name(name: str) -> bool:
    """Return whether *name* is a safe, known release asset name."""
    if not name or len(name) > MAX_ASSET_NAME:
        return False
    if not name.isascii():
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    if PurePosixPath(name).suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    return _ALLOWED_ASSET_RE.fullmatch(name) is not None


def _contained(path: Path, root: Path) -> Path | None:
    """Resolve *path* and return it only when it stays inside *root*."""
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _asset_name_problem(name: str) -> Problem | None:
    if validate_asset_name(name):
        return None
    return Problem(
        asset=name,
        kind=KIND_UNEXPECTED_NAME,
        detail="name is not on the release asset allowlist",
    )


def _size_problems(path: Path, asset: str) -> list[Problem]:
    size = path.stat().st_size
    suffix = path.suffix.lower()
    if size == 0:
        return [Problem(asset, KIND_EMPTY_FILE, "file is empty (0 bytes)")]
    problems: list[Problem] = []
    if suffix in RAW_SCAN_SUFFIXES and size < MIN_ARCHIVE_SIZE:
        problems.append(
            Problem(
                asset,
                KIND_SMALL_FILE,
                f"archive is only {size} bytes (< {MIN_ARCHIVE_SIZE})",
            )
        )
    if size > MAX_ASSET_SIZE:
        problems.append(
            Problem(
                asset,
                KIND_LARGE_FILE,
                f"file is {size} bytes (> {MAX_ASSET_SIZE} bytes)",
            )
        )
    return problems


def _unsafe_member_reason(value: str) -> str | None:
    """Return why a ZIP member path is unsafe, or ``None`` when it is safe."""
    if "\x00" in value:
        return "member path contains a NUL byte"
    if "\\" in value:
        return "member path contains a backslash"
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return "member path is absolute or empty"
    if re.match(r"[A-Za-z]:", normalized):
        return "member path starts with a drive letter"
    path = PurePosixPath(normalized)
    if ".." in path.parts:
        return "member path escapes the archive root"
    return None


def _forbidden_member_problems(value: str, asset: str) -> list[Problem]:
    problems: list[Problem] = []
    reason = _unsafe_member_reason(value)
    if reason is not None:
        problems.append(Problem(asset, KIND_UNSAFE_MEMBER, f"{value!r}: {reason}"))
        return problems

    parts = [part.lower() for part in PurePosixPath(value).parts]
    for part in parts:
        if part in FORBIDDEN_DIRECTORY_PARTS:
            problems.append(
                Problem(asset, KIND_FORBIDDEN_DIRECTORY, f"{value!r}: {part}/")
            )
            break
    if any(part.endswith(".dsym") for part in parts):
        problems.append(
            Problem(asset, KIND_FORBIDDEN_FILE, f"{value!r}: debug symbol bundle")
        )
        return problems

    basename = parts[-1] if parts else ""
    forbidden = basename in FORBIDDEN_FILE_NAMES
    forbidden = forbidden or basename.startswith(FORBIDDEN_FILE_PREFIXES)
    forbidden = forbidden or basename.startswith(".env.")
    forbidden = forbidden or basename.endswith(FORBIDDEN_FILE_SUFFIXES)
    if forbidden:
        if basename in SAFE_PEM_BASENAMES:
            return problems
        problems.append(
            Problem(asset, KIND_FORBIDDEN_FILE, f"{value!r}: credential/debug file")
        )
    return problems


def _scan_archive(path: Path, asset: str) -> tuple[list[Problem], int]:
    """List archive members and check every name; also count ``.py`` files."""
    problems: list[Problem] = []
    python_files = 0
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                problems.append(
                    Problem(
                        asset,
                        KIND_ARCHIVE_TOO_LARGE,
                        f"{len(members)} members (> {MAX_ARCHIVE_MEMBERS})",
                    )
                )
                return problems, python_files
            for member in members:
                if member.filename.lower().endswith(".py"):
                    python_files += 1
                # ``ZipInfo.filename`` normalizes "\\" to "/", which would hide
                # exactly the unsafe member names this audit must catch.
                problems.extend(_forbidden_member_problems(member.orig_filename, asset))
    except (
        OSError,
        RuntimeError,
        EOFError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        problems.append(
            Problem(
                asset,
                KIND_UNREADABLE_ARCHIVE,
                f"cannot read archive: {type(error).__name__}",
            )
        )
        return problems, python_files
    return problems, python_files


def _scan_raw_bytes(path: Path, asset: str) -> list[Problem]:
    """Stream *path* and look for credential markers without echoing them."""
    problems: list[Problem] = []
    seen: set[str] = set()
    tail = b""
    try:
        with path.open("rb") as source:
            while chunk := source.read(RAW_CHUNK_SIZE):
                sample = tail + chunk
                for marker in SECRET_MARKERS:
                    label = marker.decode("ascii")
                    if label in seen or marker not in sample:
                        continue
                    seen.add(label)
                    problems.append(
                        Problem(asset, KIND_SECRET_MARKER, f"{asset}: {label}")
                    )
                if TOKEN_LABEL not in seen and TOKEN_PATTERN.search(sample):
                    seen.add(TOKEN_LABEL)
                    problems.append(
                        Problem(asset, KIND_SECRET_MARKER, f"{asset}: {TOKEN_LABEL}")
                    )
                tail = sample[-RAW_OVERLAP:]
    except OSError as error:
        raise AuditError(f"cannot read {asset}: {error}") from error
    return problems


def _audit_file(path: Path, asset: str) -> tuple[list[Problem], int]:
    problems: list[Problem] = []
    name_problem = _asset_name_problem(asset)
    if name_problem is not None:
        problems.append(name_problem)
    problems.extend(_size_problems(path, asset))

    python_files = 0
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        archive_problems, python_files = _scan_archive(path, asset)
        problems.extend(archive_problems)
    if suffix in RAW_SCAN_SUFFIXES:
        problems.extend(_scan_raw_bytes(path, asset))
    return problems, python_files


def audit_directory(root: Path, expected_names: set[str] | None = None) -> AuditResult:
    """Audit every file in *root*; optionally compare with *expected_names*."""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise AuditError(f"not a directory: {root}")

    problems: list[Problem] = []
    scanned = 0
    python_files = 0
    actual_names: set[str] = set()

    for entry in sorted(resolved_root.iterdir(), key=lambda item: item.name):
        resolved = _contained(entry, resolved_root)
        if resolved is None:
            problems.append(
                Problem(
                    entry.name,
                    KIND_UNSAFE_PATH,
                    "entry resolves outside the audited directory",
                )
            )
            continue
        if not resolved.is_file():
            problems.append(
                Problem(entry.name, KIND_UNEXPECTED_ENTRY, "not a regular file")
            )
            continue
        scanned += 1
        actual_names.add(entry.name)
        file_problems, file_python = _audit_file(resolved, entry.name)
        problems.extend(file_problems)
        python_files += file_python

    if expected_names is not None:
        for name in sorted(expected_names - actual_names):
            problems.append(
                Problem(
                    name,
                    KIND_MISSING_ASSET,
                    "listed in the release JSON but absent from the directory",
                )
            )
        for name in sorted(actual_names - expected_names):
            problems.append(
                Problem(
                    name,
                    KIND_ORPHAN_FILE,
                    "present in the directory but absent from the release JSON",
                )
            )

    return AuditResult(scanned=scanned, problems=problems, python_files=python_files)


def audit_files(assets: Sequence[tuple[str, Path]]) -> AuditResult:
    """
    Audit an explicit ``(release name, local path)`` list.

    Release assets do not necessarily live in one directory (the APK comes
    from the Gradle output tree, the installer from ``dist/``), so the upload
    set is audited directly instead of copying hundreds of megabytes into a
    staging directory first.
    """
    problems: list[Problem] = []
    scanned = 0
    python_files = 0
    seen: set[str] = set()

    for name, path in assets:
        if name in seen:
            problems.append(
                Problem(name, KIND_DUPLICATE_ASSET, "asset name appears twice")
            )
            continue
        seen.add(name)
        resolved = path.resolve()
        if not resolved.is_file():
            problems.append(
                Problem(name, KIND_MISSING_ASSET, f"file not found: {path}")
            )
            continue
        scanned += 1
        file_problems, file_python = _audit_file(resolved, name)
        problems.extend(file_problems)
        python_files += file_python

    return AuditResult(scanned=scanned, problems=problems, python_files=python_files)


def load_release_asset_names(path: Path) -> set[str]:
    """Read the asset names from a GitHub release payload."""
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read release JSON {path}: {error}") from error

    assets: Any
    if isinstance(payload, dict):
        assets = payload.get("assets")
    elif isinstance(payload, list):
        assets = payload
    else:
        raise AuditError("release JSON must be an object with an 'assets' list")
    if not isinstance(assets, list):
        raise AuditError("release JSON must contain an 'assets' list")

    names: set[str] = set()
    for item in assets:
        name: Any = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            raise AuditError("release JSON contains an asset without a name")
        names.add(name)
    return names


def _payload(result: AuditResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "scanned": result.scanned,
        "problems": [
            {"asset": problem.asset, "kind": problem.kind, "detail": problem.detail}
            for problem in result.problems
        ],
        # Informational only: PyInstaller may collect a handful of .py files.
        "python_files": result.python_files,
    }


def _print_report(result: AuditResult) -> None:
    for problem in result.problems:
        print(f"PROBLEM [{problem.kind}] {problem.asset}: {problem.detail}")
    if result.ok:
        print(f"OK: {result.scanned} release asset(s) passed the audit")
    else:
        print(
            f"FAILED: {len(result.problems)} problem(s) "
            f"across {result.scanned} release asset(s)"
        )
    print(
        f"Python files inside archives: {result.python_files} "
        "(informational, review manually)"
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit built release assets for leaks and unsafe contents."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="release directory to audit",
    )
    parser.add_argument(
        "--release-json",
        type=Path,
        default=None,
        help="GitHub release payload to compare the asset set against",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a machine-readable JSON report",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        expected = (
            load_release_asset_names(args.release_json)
            if args.release_json is not None
            else None
        )
        result = audit_directory(args.dir, expected)
    except (AuditError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR

    if args.json_output:
        print(json.dumps(_payload(result), indent=2))
    else:
        _print_report(result)
    return EXIT_OK if result.ok else EXIT_PROBLEMS


if __name__ == "__main__":
    raise SystemExit(main())
