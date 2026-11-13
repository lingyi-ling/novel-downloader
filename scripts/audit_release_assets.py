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

``archive text members``
    Text members inside every readable ZIP/APK -- and inside an ``.exe`` that
    is really a ZIP (PyInstaller one-file builds are) -- are streamed line by
    line through :mod:`scripts.scan_secrets`, so the rules and path allowlist
    cannot drift from the repository scanner.  A hit is reported as
    ``asset: member:line: rule`` and never includes the matched value.
    Binary members (NUL in the first probe), members above
    :data:`MAX_MEMBER_SCAN_BYTES`, nested archives (never extracted) and
    everything beyond the per-asset/total byte budgets are skipped.  An
    ``.exe`` that is not a ZIP is simply not opened as an archive.

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
import importlib.util
import json
import re
import stat
import sys
import types
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

#: Member-content scanning is bounded three ways: a single member, one asset
#: and the whole audit run.  The per-member cap matches ``scan_secrets`` so a
#: member is treated exactly like a tracked file; the asset and total caps keep
#: a bundle full of large text files from turning the audit into a slow scan.
MAX_MEMBER_SCAN_BYTES = 2 * 1024 * 1024
MAX_ASSET_SCAN_BYTES = 64 * 1024 * 1024
MAX_TOTAL_SCAN_BYTES = 256 * 1024 * 1024

ALLOWED_SUFFIXES = frozenset({".zip", ".exe", ".apk", ".txt", ".json"})
ARCHIVE_SUFFIXES = frozenset({".zip", ".apk"})
RAW_SCAN_SUFFIXES = frozenset({".zip", ".exe", ".apk"})
#: Nested archives are never extracted: a release asset has no reason to ship
#: one, and recursing would let a small outer file hide an unbounded payload.
NESTED_ARCHIVE_SUFFIXES = frozenset({".zip", ".exe", ".apk"})


def _load_secret_scanner() -> types.ModuleType:
    """Load ``scan_secrets.py`` from this script's directory.

    The auditor is itself imported by path (from ``release.py`` and the tests),
    so a plain ``import scan_secrets`` would depend on the caller's
    ``sys.path``.  Importing the sibling file explicitly keeps the rule set and
    the path allowlist in exactly one place.
    """
    path = Path(__file__).resolve().with_name("scan_secrets.py")
    spec = importlib.util.spec_from_file_location("_audit_secret_scanner", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


_SECRET_SCANNER = _load_secret_scanner()
BINARY_PROBE_BYTES = _SECRET_SCANNER.BINARY_PROBE_BYTES

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
#: other ``*.pem`` file is still rejected.  The exemption is verified against
#: the bundle's bytes: naming a private key ``cacert.pem`` does not hide it.
SAFE_PEM_BASENAMES = frozenset({"cacert.pem"})
#: A real CA bundle is a few hundred KiB.  Anything larger cannot be verified
#: cheaply, so it is not granted the exemption (fail closed).
PEM_SCAN_LIMIT = 4 * 1024 * 1024
_PRIVATE_KEY_MARKERS = (b"PRIVATE KEY",)

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


class _ScanBudget:
    """Byte budget for member-content scanning.

    ``asset_left`` is reset for every asset; ``total_left`` spans the whole
    audit run.  Both are mutable on purpose: a shared instance threads the
    total limit through :func:`audit_directory` / :func:`audit_files`.
    """

    __slots__ = ("asset_left", "total_left")

    def __init__(self) -> None:
        self.asset_left = MAX_ASSET_SCAN_BYTES
        self.total_left = MAX_TOTAL_SCAN_BYTES

    def start_asset(self) -> None:
        self.asset_left = MAX_ASSET_SCAN_BYTES

    def take(self, amount: int) -> int:
        """Consume up to *amount* bytes; return how many may be inspected."""
        allowed = min(amount, self.asset_left, self.total_left)
        self.asset_left -= allowed
        self.total_left -= allowed
        return allowed

    @property
    def exhausted(self) -> bool:
        return self.asset_left <= 0 or self.total_left <= 0


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


def _forbidden_member_problems(
    value: str, asset: str, *, safe_pem: bool = False
) -> list[Problem]:
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
        if safe_pem and basename in SAFE_PEM_BASENAMES:
            return problems
        problems.append(
            Problem(asset, KIND_FORBIDDEN_FILE, f"{value!r}: credential/debug file")
        )
    return problems


def _pem_contains_private_key(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo
) -> bool:
    """Whether an exempted ``cacert.pem`` actually hides a private key.

    Unreadable or oversized members are treated as unsafe so the exemption
    can never become a way to smuggle a key past the audit.
    """
    if member.file_size > PEM_SCAN_LIMIT:
        return True
    tail = b""
    consumed = 0
    try:
        with archive.open(member) as handle:
            while chunk := handle.read(RAW_CHUNK_SIZE):
                consumed += len(chunk)
                if consumed > PEM_SCAN_LIMIT:
                    # A member whose declared size lies must not stream forever.
                    return True
                sample = tail + chunk
                if any(marker in sample for marker in _PRIVATE_KEY_MARKERS):
                    return True
                tail = sample[-RAW_OVERLAP:]
    except (OSError, RuntimeError, EOFError, ValueError, zipfile.BadZipFile):
        return True
    return False


def _safe_pem_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bool:
    """Whether *member* earns the public-CA-bundle exemption."""
    basename = PurePosixPath(member.filename).name.lower()
    if basename not in SAFE_PEM_BASENAMES:
        return False
    return not _pem_contains_private_key(archive, member)


def _scan_member_line(
    raw: bytes, member_path: str, asset: str, line_no: int
) -> list[Problem]:
    """Scan one member line with the repository scanner's exact rule set."""
    text = raw.decode("utf-8", "replace").rstrip("\r")
    # ``scan_text`` applies ``RULES``, the placeholder heuristics and the path
    # allowlist.  Its own line number is always 1 here because this is one line.
    return [
        Problem(asset, KIND_SECRET_MARKER, f"{member_path}:{line_no}: {finding.rule}")
        for finding in _SECRET_SCANNER.scan_text(member_path, text + "\n")
    ]


def _drain_member_lines(
    buffer: bytearray,
    line_no: int,
    member_path: str,
    asset: str,
    problems: list[Problem],
    *,
    final: bool,
) -> int:
    """Emit every complete line in *buffer*; optionally flush the remainder."""
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            break
        raw = bytes(buffer[:newline])
        del buffer[: newline + 1]
        line_no += 1
        problems.extend(_scan_member_line(raw, member_path, asset, line_no))
    if final and buffer:
        raw = bytes(buffer)
        buffer.clear()
        line_no += 1
        problems.extend(_scan_member_line(raw, member_path, asset, line_no))
    return line_no


def _scan_member_stream(
    handle: Any,
    member_path: str,
    asset: str,
    budget: _ScanBudget,
) -> list[Problem]:
    """Stream a member as text and scan it line by line.

    The first :data:`BINARY_PROBE_BYTES` decide text versus binary, exactly as
    ``scan_secrets`` does for tracked files.  Reading stays incremental and the
    shared budget stops a member whose declared size lies.
    """
    problems: list[Problem] = []
    probe = handle.read(BINARY_PROBE_BYTES)
    if not probe:
        return problems
    probe = probe[: budget.take(len(probe))]
    if b"\x00" in probe:
        return problems

    buffer = bytearray(probe)
    line_no = _drain_member_lines(buffer, 0, member_path, asset, problems, final=False)
    while not budget.exhausted:
        chunk = handle.read(RAW_CHUNK_SIZE)
        if not chunk:
            break
        allowed = budget.take(len(chunk))
        buffer.extend(chunk[:allowed])
        line_no = _drain_member_lines(
            buffer, line_no, member_path, asset, problems, final=False
        )
        if allowed < len(chunk):
            break
    _drain_member_lines(buffer, line_no, member_path, asset, problems, final=True)
    return problems


def _scan_member_secrets(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    asset: str,
    budget: _ScanBudget,
) -> list[Problem]:
    """Scan one archive member's text content, or skip it with a reason."""
    member_path = member.orig_filename
    if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
        return []
    suffix = PurePosixPath(member_path.replace("\\", "/")).suffix.lower()
    if suffix in NESTED_ARCHIVE_SUFFIXES:
        # Never extract a nested archive: one level is the whole contract.
        return []
    if member.file_size == 0 or member.file_size > MAX_MEMBER_SCAN_BYTES:
        return []
    if budget.exhausted:
        return []
    try:
        with archive.open(member) as handle:
            return _scan_member_stream(handle, member_path, asset, budget)
    except (OSError, RuntimeError, EOFError, ValueError, zipfile.BadZipFile) as error:
        # A member that cannot be read is reported, not silently treated as
        # clean: otherwise a corrupt entry would hide its content entirely.
        return [
            Problem(
                asset,
                KIND_UNREADABLE_ARCHIVE,
                f"{member_path!r}: cannot read member: {type(error).__name__}",
            )
        ]


def _scan_archive(
    path: Path,
    asset: str,
    budget: _ScanBudget,
    *,
    tolerate_not_zip: bool = False,
) -> tuple[list[Problem], int]:
    """List archive members, check every name and scan text content.

    ``tolerate_not_zip`` is used for ``.exe``: a PyInstaller one-file build is
    a ZIP appended to a native stub and opens fine, while an installer that is
    not a ZIP at all is not an archive error -- it is simply skipped.
    """
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
                problems.extend(
                    _forbidden_member_problems(
                        member.orig_filename,
                        asset,
                        safe_pem=_safe_pem_member(archive, member),
                    )
                )
                if stat.S_ISLNK(member.external_attr >> 16):
                    # Symlinks in a release archive point somewhere else on
                    # the installing machine; nothing legitimate needs one.
                    problems.append(
                        Problem(
                            asset,
                            KIND_UNSAFE_MEMBER,
                            f"{member.orig_filename!r}: symbolic link member",
                        )
                    )
                    continue
                problems.extend(_scan_member_secrets(archive, member, asset, budget))
    except (
        OSError,
        RuntimeError,
        EOFError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        if tolerate_not_zip and isinstance(error, zipfile.BadZipFile):
            return problems, python_files
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
                    problems.append(Problem(asset, KIND_SECRET_MARKER, label))
                if TOKEN_LABEL not in seen and TOKEN_PATTERN.search(sample):
                    seen.add(TOKEN_LABEL)
                    problems.append(Problem(asset, KIND_SECRET_MARKER, TOKEN_LABEL))
                tail = sample[-RAW_OVERLAP:]
    except OSError as error:
        raise AuditError(f"cannot read {asset}: {error}") from error
    return problems


def _audit_file(
    path: Path, asset: str, budget: _ScanBudget
) -> tuple[list[Problem], int]:
    problems: list[Problem] = []
    name_problem = _asset_name_problem(asset)
    if name_problem is not None:
        problems.append(name_problem)
    problems.extend(_size_problems(path, asset))

    budget.start_asset()
    python_files = 0
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        archive_problems, python_files = _scan_archive(path, asset, budget)
        problems.extend(archive_problems)
    elif suffix == ".exe":
        archive_problems, python_files = _scan_archive(
            path, asset, budget, tolerate_not_zip=True
        )
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
    budget = _ScanBudget()

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
        file_problems, file_python = _audit_file(resolved, entry.name, budget)
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
    budget = _ScanBudget()

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
        file_problems, file_python = _audit_file(resolved, name, budget)
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
