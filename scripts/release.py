#!/usr/bin/env python3
"""One-command release for novel-downloader.

Usage::

    .venv/Scripts/python.exe scripts/release.py --dry-run
    .venv/Scripts/python.exe scripts/release.py
    .venv/Scripts/python.exe scripts/release.py --version 1.3.0
    .venv/Scripts/python.exe scripts/release.py --skip-build --skip-android
    .venv/Scripts/python.exe scripts/release.py --skip-installer
    .venv/Scripts/python.exe scripts/release.py --skip-secret-scan
    GITHUB_TOKEN=... python scripts/release.py --repo owner/name

What a full run does, in order:

1. Preflight, before any build: read the version from
   ``src/novel_downloader/__init__.py`` (or ``--version``); prove that the
   Android ``versionName``, the derived ``v<version>`` tag and the **top**
   ``CHANGELOG.md`` section (``## v<version> (<date>)``, non-empty) all agree.
   That section is the release notes.  Each failure raises with the file to
   edit, so a forgotten changelog entry costs seconds instead of a full build.
   The last preflight gate scans every Git-tracked file with
   ``scripts/scan_secrets.py``: a credential that reached the working tree
   would otherwise be bundled and published, and would only be noticed after
   the build.  A finding (or a scan error) aborts with exit 2 and reports only
   ``path:line: rule``, never the matched text.  ``--skip-secret-scan`` turns
   that gate off; it exists for a verified false positive and is not
   recommended, because the build then runs unscanned.
2. Build the three PyInstaller bundles by invoking
   ``scripts/build_release.py --targets cli,api,desktop`` (same validation,
   zipping and SHA256SUMS handling as a manual build).
3. Build the Android release APK with ``gradlew assembleRelease``.  When the
   signing material (``android/keystore.properties`` + keystore file) is
   missing, this step warns and is skipped instead of failing the release.
   For the ``desktop`` target the Inno Setup installer is **required**: the
   build refuses to start when ISCC cannot be found, and the collected assets
   must contain ``NovelDownloader-setup-<version>-x64.exe``.  Pass
   ``--skip-installer`` to publish the portable zip only; ``latest.json`` then
   advertises no ``windows-x64-installer`` platform, and the portable zip must
   be present as the fallback.
4. Merge the checksums of the APK (and the Inno Setup installer, when present)
   into the build's ``SHA256SUMS.txt``.
5. Create the ``v<version>`` tag locally and push it.  Both steps are skipped
   when the tag already exists locally / on the remote.  An existing tag that
   points at another commit is a hard error, never silently reused.  The push
   uses git's own credential chain, or a per-process ``http.extraheader``
   built from ``GITHUB_TOKEN`` / ``GH_TOKEN``; the REST token comes from the
   same variables or from ``git credential fill`` (protocol=https,
   host=github.com).  No credential is ever hardcoded, printed, or placed in
   a command line.
6. Create the GitHub Release as a **draft**, upload every asset, verify the
   published digests, and only then flip it to published.  A failure at any
   point leaves a resumable draft rather than a half-populated public release.
   An asset whose remote digest matches the local SHA-256 is skipped; one
   that differs is deleted and re-uploaded.  Re-running a finished release
   therefore does nothing but a GET.

``--dry-run`` prints the whole plan (notes, commands, assets and the platforms
``latest.json`` would advertise) and executes nothing: no subprocess, no
network.  ``--skip-build`` reuses an existing
``dist/release/novel-downloader-<version>-*/`` directory, which is how a run
that died midway (e.g. network drop) is resumed without rebuilding; assets
already uploaded in that run are skipped by SHA-256 on the next attempt.

Exit codes: 0 success; 2 for a ``ReleaseError`` (bad input, failed build,
API error) and for argparse usage errors.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

PROJECT_NAME = "novel-downloader"
DEFAULT_TARGETS = ("cli", "api", "desktop")
#: The desktop target's installed-user payload, built by ``build_desktop.py``.
INSTALLER_NAME_TEMPLATE = "NovelDownloader-setup-{version}-x64.exe"
GITHUB_API = "https://api.github.com"
GITHUB_HOST = "github.com"
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
CHUNK_SIZE = 1024 * 1024
HTTP_TIMEOUT = 120.0
#: Uploads move tens of megabytes; a slow link can exceed the API timeout.
UPLOAD_TIMEOUT = 900.0
#: Attempts per asset upload, with the delays below between them.
UPLOAD_ATTEMPTS = 4
UPLOAD_BACKOFF = (4.0, 8.0, 16.0)
CREDENTIAL_TIMEOUT = 30.0
#: ``releases/tags/<tag>`` hides drafts; the list endpoint finds them.
RELEASE_LIST_PAGE_SIZE = 100
RELEASE_LIST_PAGES = 10
#: ``git ls-remote`` / ``git push`` touch github.com (a different service from
#: api.github.com).  A blocked host can hang them for minutes, so bound both
#: and fall back to the REST API instead.
GIT_REMOTE_TIMEOUT = 60.0
PUSH_TIMEOUT = 300.0
GRADLE_TIMEOUT = 2700.0
# PyInstaller builds of three bundles take tens of minutes: no timeout.
BUILD_TIMEOUT: float | None = None
#: GitHub rejects release assets larger than 2 GiB.  Checking before the
#: upload turns a 900-second failing transfer into an immediate error.
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
#: Characters git accepts in a tag name, plus the shape of a version string.
_VERSION_TAG_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]*")
#: Any ``## `` heading; ``.`` does not cross the line, so CRLF is split off.
_CHANGELOG_HEADING_RE = re.compile(r"^##[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)
#: A release section starts with ``v`` followed by a digit (``v1.2.3``).
_VERSION_HEADING_RE = re.compile(r"^v[0-9]")
_SUMS_LINE_RE = re.compile(r"^(?P<sha>[0-9a-fA-F]{64})\s+(?P<name>\S.*)$")
_REMOTE_SLUG_RE = re.compile(
    r"github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)"
    r"(?:\.git)?/?\s*$"
)
_TOKEN_IN_URL_RE = re.compile(r"x-access-token:[^@\s]+@")
#: ``https://user:password@host/...`` (any userinfo) in output or a remote URL.
_USERINFO_IN_URL_RE = re.compile(r"(?<=://)[^/@\s]+@")
_APK_GLOB = "app/build/outputs/apk/release/*.apk"

#: Printed when github.com:443 is unreachable.  ``scripts/push_via_api.py``
#: pushes the *commit* (not just the tag) through api.github.com and never
#: force-updates a ref, but it is never run automatically from here.
PUSH_API_HINT = (
    "hint: if github.com:443 is blocked, push the commit with "
    "`python scripts/push_via_api.py` (api.github.com only, never force-updates)"
)


class ReleaseError(RuntimeError):
    """A release step failed; the message is safe to print."""


class TransientAPIError(ReleaseError):
    """A failure worth retrying: network drop, timeout, 5xx or rate limit."""


@dataclass(frozen=True)
class AssetSpec:
    """One file to upload, under the name it gets on the release."""

    path: Path
    name: str
    sha256: str


@dataclass(frozen=True)
class Reply:
    """One GitHub REST reply: status plus the parsed JSON body (or None)."""

    status: int
    payload: Any


# ---------------------------------------------------------------------------
# Version, notes and repository discovery
# ---------------------------------------------------------------------------


def read_version(repo_root: Path) -> str:
    """Return ``__version__`` from the package ``__init__`` (no imports)."""
    init_py = repo_root / "src" / "novel_downloader" / "__init__.py"
    if not init_py.is_file():
        raise ReleaseError(f"package version file not found: {init_py}")
    match = _VERSION_RE.search(init_py.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError(f"no __version__ string in {init_py}")
    return match.group(1)


def check_android_version(repo_root: Path, version: str) -> None:
    """
    Refuse to release when the Android version drifted from the package.

    ``versionCode``/``versionName`` are a hand-maintained copy of
    ``__version__``; a mismatch ships an APK that reports a different version
    than the manifest and never updates.
    """
    gradle = repo_root / "android" / "app" / "build.gradle.kts"
    if not gradle.is_file():
        return
    text = gradle.read_text(encoding="utf-8")
    match = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    if match is None:
        raise ReleaseError(f"no versionName in {gradle}")
    name = match.group(1).strip()
    if name != version:
        raise ReleaseError(
            f"android versionName {name!r} does not match __version__ "
            f"{version!r}; update android/app/build.gradle.kts"
        )
    code = re.search(r"versionCode\s*=\s*(\d+)", text)
    if code is None or int(code.group(1)) <= 0:
        raise ReleaseError(f"invalid versionCode in {gradle}")


def _secret_scanner() -> Any:
    """Import ``scripts/scan_secrets.py`` once (same pattern as the auditor)."""
    global _SECRET_SCANNER
    if _SECRET_SCANNER is None:
        spec = importlib.util.spec_from_file_location(
            "release_secret_scanner",
            SCRIPTS_DIR / "scan_secrets.py",
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ReleaseError("cannot load scripts/scan_secrets.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SECRET_SCANNER = module
    return _SECRET_SCANNER


_SECRET_SCANNER: Any = None


def scan_release_secrets(repo_root: Path) -> None:
    """
    Refuse to release when a tracked file contains a credential.

    Uses ``scripts/scan_secrets.py``'s repository scan (tracked files, their
    working-tree contents), which is the input a source bundle is built from.
    It runs before the token lookup, the worktree guard and the first build so
    a leaked key costs seconds instead of a finished (and potentially
    published) release.  The error lists only ``path:line: rule`` -- never the
    matched value -- so it is safe to paste into an issue.

    :raises ReleaseError: when the scan cannot run or finds anything.
    """
    try:
        findings = _secret_scanner().scan_repository(repo_root)
    except (OSError, RuntimeError) as exc:
        raise ReleaseError(f"tracked-file secret scan failed: {exc}") from exc
    if findings:
        shown = "; ".join(
            f"{item.path}:{item.line}: {item.rule}" for item in findings[:10]
        )
        extra = f" (+{len(findings) - 10} more)" if len(findings) > 10 else ""
        raise ReleaseError(
            f"tracked-file secret scan found {len(findings)} potential "
            f"credential(s): {shown}{extra}; remove or rotate them before "
            "releasing (pass --skip-secret-scan only for a verified false "
            "positive)"
        )
    print("==> secret scan clean: no credentials in tracked files")


def check_release_tag(version: str, tag: str) -> None:
    """
    Prove the release tag really is ``v<version>`` and is a legal git ref.

    The tag is derived from the version, but ``--version`` accepts raw input:
    a trailing space or a slash produces a tag git rejects.  Without this
    check that rejection happens *after* the bundles and the APK are built,
    wasting the whole run.  Pure string work, so ``--dry-run`` stays offline.
    """
    if tag != f"v{version}":
        raise ReleaseError(
            f"release tag {tag!r} does not match version {version!r} "
            f"(expected 'v{version}')"
        )
    if (
        _VERSION_TAG_RE.fullmatch(version) is None
        or ".." in version
        or version.endswith((".", ".lock"))
    ):
        raise ReleaseError(
            f"version {version!r} cannot be used as the git tag {tag!r}; use "
            "letters, digits, dots, dashes, plus signs or underscores "
            "(e.g. 1.2.3 or 1.2.3-beta.1)"
        )


def require_clean_worktree(repo_root: Path) -> None:
    """Refuse to tag a commit whose working tree has uncommitted changes."""
    proc = _run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        check=False,
    )
    if proc.returncode != 0:
        detail = _redact((proc.stderr or proc.stdout or "").strip(), ())
        if "not a git repository" in detail.lower():
            return  # an exported tree has no git state to check
        raise ReleaseError(
            "cannot check the working tree: git status exited with "
            f"{proc.returncode}: {detail or 'no output'}"
        )
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        preview = "; ".join(dirty[:5])
        raise ReleaseError(
            f"working tree has {len(dirty)} uncommitted change(s): {preview}"
            " (commit them, or pass --allow-dirty)"
        )


def extract_changelog_notes(changelog: Path, version: str) -> str:
    """
    Return the body of the ``## v<version>`` section of *changelog*.

    The header line itself (``## v1.2.0 (2026-10-31)``) is dropped; the body
    ends at the next ``##`` heading of any kind.  ``version`` may be
    ``Unreleased``.  A version whose section is missing raises
    :class:`ReleaseError` listing the sections that do exist, so a forgotten
    changelog entry stops the release before anything is built.
    """
    if not changelog.is_file():
        raise ReleaseError(f"CHANGELOG not found: {changelog}")
    text = changelog.read_text(encoding="utf-8")
    header = re.compile(
        rf"^##[ \t]+(?:v)?{re.escape(version)}(?![\w.-])[^\n]*", re.MULTILINE
    )
    match = header.search(text)
    if match is None:
        available = [
            line.strip() for line in text.splitlines() if re.match(r"^##[ \t]+\S", line)
        ]
        raise ReleaseError(
            f"CHANGELOG.md has no section for version {version!r} "
            f"(available sections: {', '.join(available) or 'none'})"
        )

    body: list[str] = []
    for line in text[match.end() :].splitlines():
        if re.match(r"^##[ \t]", line):
            break
        body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def _version_section_titles(text: str) -> list[str]:
    """Every ``## v<digit>...`` heading, top to bottom."""
    return [
        match.group(1)
        for match in _CHANGELOG_HEADING_RE.finditer(text)
        if _VERSION_HEADING_RE.match(match.group(1))
    ]


def check_changelog_entry(changelog: Path, version: str) -> str:
    """
    Validate (and return) the CHANGELOG section for *version*.

    The release notes come from the changelog, so this runs before any build:
    :func:`extract_changelog_notes` alone accepts a bare ``## 1.2.3`` heading
    and an empty body, and an empty body used to surface as a warning only
    after a successful build.  This gate is stricter:

    * the heading is the project's documented form ``## v<version> (<date>)``;
    * it is the **topmost** release section, so releasing an older version
      cannot silently publish another version's notes;
    * its body is not blank.

    ``version`` may be a prerelease (``1.2.3-beta.1``); the heading must match
    it exactly, never the stable base version.
    """
    if not changelog.is_file():
        raise ReleaseError(f"CHANGELOG not found: {changelog}")
    titles = _version_section_titles(changelog.read_text(encoding="utf-8"))
    wanted = re.compile(rf"^v{re.escape(version)}[ \t]+\([^)]+\)[ \t]*$")
    available = ", ".join(titles) or "none"
    if not any(wanted.match(title) for title in titles):
        raise ReleaseError(
            f"CHANGELOG.md has no section `## v{version} (` for this release; "
            f"add `## v{version} (YYYY-MM-DD)` at the top with the release "
            f"notes (available version sections: {available})"
        )
    if not wanted.match(titles[0]):
        raise ReleaseError(
            f"CHANGELOG.md's newest version section is `## {titles[0]}`, but "
            f"this release is v{version}; move `## v{version} (...)` to the "
            "top before releasing"
        )
    notes = extract_changelog_notes(changelog, version)
    if not notes.strip():
        raise ReleaseError(
            f"CHANGELOG.md section `## {titles[0]}` is empty; add the release "
            "notes under it before releasing"
        )
    return notes


def parse_remote_slug(url: str) -> str | None:
    """Return ``owner/repo`` for a github.com remote URL, else ``None``."""
    match = _REMOTE_SLUG_RE.search(url.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}"


def resolve_slug(repo_root: Path) -> str:
    """Derive ``owner/repo`` from the ``origin`` remote."""
    proc = _run(["git", "remote", "get-url", "origin"], cwd=repo_root, check=False)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        raise ReleaseError(
            "no origin remote configured; pass --repo OWNER/NAME explicitly"
        )
    slug = parse_remote_slug(lines[0])
    if slug is None:
        raise ReleaseError(
            "cannot derive OWNER/NAME from origin URL "
            f"{_redact(lines[0], ())!r}; pass --repo OWNER/NAME explicitly"
        )
    return slug


# ---------------------------------------------------------------------------
# Subprocess plumbing (tests monkeypatch the module-level ``subprocess``)
# ---------------------------------------------------------------------------


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """
    Remove credentials from *text* before it is printed or logged.

    Covers both the tokens this script knows about and userinfo embedded in a
    remote URL (``https://user:pat@github.com/...``), which git may echo.
    """
    text = _USERINFO_IN_URL_RE.sub("***@", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _display_command(
    command: list[str] | tuple[str, ...], secrets: tuple[str, ...] = ()
) -> str:
    """A printable form of *command* with embedded tokens redacted."""
    rendered = _TOKEN_IN_URL_RE.sub("x-access-token:***@", " ".join(command))
    return _redact(rendered, secrets)


def _run(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """
    Run *command* capturing UTF-8 output; raise ReleaseError on failure.

    *secrets* are redacted from every message this function builds (including
    the child's own stderr, which may echo the remote URL or a header value).
    """
    env: dict[str, str] | None = None
    if extra_env:
        env = {**os.environ, **extra_env}
    try:
        proc = subprocess.run(
            list(command),
            cwd=None if cwd is None else str(cwd),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(
            f"command timed out after {timeout}s: {_display_command(command, secrets)}"
        ) from exc
    if check and proc.returncode != 0:
        # stderr/stdout may be None on CompletedProcess, so guard the strip.
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        raise ReleaseError(
            f"command failed with exit code {proc.returncode} "
            f"({_display_command(command, secrets)}): "
            + _redact(" | ".join(detail[-8:]), secrets)
        )
    return proc


def _announce(
    command: list[str] | tuple[str, ...], secrets: tuple[str, ...] = ()
) -> None:
    print(f"==> {_display_command(command, secrets)}")


# ---------------------------------------------------------------------------
# Build artifacts: PyInstaller bundles, Android APK, checksums
# ---------------------------------------------------------------------------


def platform_tag() -> str:
    """A release-friendly ``<os>-<arch>`` tag; mirrors build_release.py."""
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


def _sha256_file(path: Path) -> str:
    """Hex SHA-256 digest of *path*, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_size(num_bytes: int) -> str:
    """Format a byte count as KiB/MiB/GiB (same style as build_release.py)."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - unreachable


def find_release_dir(repo_root: Path, version: str) -> Path | None:
    """Locate ``dist/release/novel-downloader-<version>-<platform>/``."""
    out_root = repo_root / "dist" / "release"
    exact = out_root / f"{PROJECT_NAME}-{version}-{platform_tag()}"
    if exact.is_dir():
        return exact
    if not out_root.is_dir():
        return None
    matches = sorted(
        path for path in out_root.glob(f"{PROJECT_NAME}-{version}-*") if path.is_dir()
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ReleaseError(
            "ambiguous release directories for "
            f"{version}: {', '.join(str(path) for path in matches)}; "
            "remove the stale one(s) under dist/release/"
        )
    return None


def _read_release_manifest(release_dir: Path) -> dict[str, Any] | None:
    """Parse ``manifest.json`` from a build output directory (``None`` if absent)."""
    path = release_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReleaseError(f"{path}: expected a JSON object")
    return document


def verify_release_dir(release_dir: Path, version: str, targets: list[str]) -> None:
    """
    Prove a build output directory is complete before anything is published.

    ``--skip-build`` deliberately reuses an earlier directory, but a directory
    left behind by a failed or interrupted build can otherwise look usable:
    the missing bundle simply gets no ``latest.json`` entry, and the release
    silently ships without it.  ``build_release.py`` records exactly what it
    built in ``manifest.json`` (targets, artifacts and failures), so that file
    is the completeness contract:

    * the manifest was written for this version and reports no failures;
    * every requested target has at least one artifact whose file is present;
    * no extra file is sitting in the directory (a stale archive from another
      version would otherwise be uploaded as part of this release).
    """
    manifest = _read_release_manifest(release_dir)
    if manifest is None:
        raise ReleaseError(
            f"{release_dir} has no manifest.json; rebuild the release "
            "without --skip-build so its contents can be verified"
        )
    if manifest.get("version") != version:
        raise ReleaseError(
            f"{release_dir}/manifest.json is for version "
            f"{manifest.get('version')!r}, expected {version!r}"
        )
    failures = manifest.get("failures")
    if failures:
        detail = "; ".join(
            f"{item.get('target', '?')}: {item.get('message', 'failed')}"
            for item in failures
            if isinstance(item, dict)
        )
        raise ReleaseError(
            f"{release_dir}/manifest.json records a failed build"
            + (f": {detail}" if detail else "")
        )

    built_targets = manifest.get("targets")
    if not isinstance(built_targets, list):
        raise ReleaseError(f"{release_dir}/manifest.json has no targets list")
    missing_targets = [target for target in targets if target not in built_targets]
    if missing_targets:
        raise ReleaseError(
            f"{release_dir} was not built for target(s): "
            + ", ".join(missing_targets)
            + "; rebuild without --skip-build or pass the matching --targets"
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseError(f"{release_dir}/manifest.json lists no artifacts")

    files: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ReleaseError(f"{release_dir}/manifest.json has a malformed artifact")
        name = item.get("file")
        target = item.get("target")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ReleaseError(
                f"{release_dir}/manifest.json has an unsafe artifact name: {name!r}"
            )
        path = release_dir / name
        if path.is_symlink():
            raise ReleaseError(f"release artifact {name!r} is a symlink")
        if not path.is_file():
            raise ReleaseError(
                f"release artifact {name!r} is missing from {release_dir}"
            )
        if path.stat().st_size == 0:
            raise ReleaseError(f"release artifact {name!r} is empty")
        size = item.get("size")
        if isinstance(size, int) and size != path.stat().st_size:
            raise ReleaseError(
                f"release artifact {name!r} is {path.stat().st_size} bytes, but "
                f"manifest.json recorded {size}"
            )
        # Artifacts for other targets are left alone: --targets says what must
        # be present, and a directory built for more targets is still a
        # complete, consistent build.
        files.add(name)

    for target in targets:
        if not any(
            isinstance(item, dict) and item.get("target") == target
            for item in artifacts
        ):
            raise ReleaseError(
                f"{release_dir}/manifest.json has no artifact for target {target!r}"
            )

    metadata = {"SHA256SUMS.txt", "manifest.json", "latest.json"}
    extras = sorted(
        path.name
        for path in release_dir.iterdir()
        if (path.is_file() or path.is_symlink())
        and path.name not in metadata
        and path.name not in files
    )
    if extras:
        raise ReleaseError(
            f"{release_dir} contains file(s) not in manifest.json: "
            + ", ".join(extras)
            + "; remove them or rebuild the release"
        )
    print(
        f"==> Release directory verified: {len(files)} artifact(s) for "
        f"target(s) {', '.join(targets)}"
    )


def _parse_properties(path: Path) -> dict[str, str]:
    """Parse a minimal ``key=value`` Java properties file."""
    props: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def android_signing_available(repo_root: Path) -> tuple[bool, str]:
    """Return ``(available, reason)`` for the release signing material."""
    android_dir = repo_root / "android"
    props_path = android_dir / "keystore.properties"
    if not props_path.is_file():
        return False, f"{props_path} not found"
    props = _parse_properties(props_path)
    required = ("storeFile", "storePassword", "keyAlias", "keyPassword")
    missing = [key for key in required if not props.get(key)]
    if missing:
        return False, f"keystore.properties is missing keys: {', '.join(missing)}"
    store = Path(props["storeFile"])
    if not store.is_absolute():
        store = android_dir / store
    if not store.is_file():
        return False, f"keystore file not found: {store}"
    return True, f"release signing configured ({store.name})"


def gradle_command(repo_root: Path) -> list[str]:
    """The assembleRelease command line, as a list for subprocess."""
    android_dir = repo_root / "android"
    if os.name == "nt":
        wrapper = android_dir / "gradlew.bat"
        return [str(wrapper), "assembleRelease", "--no-daemon", "--console=plain"]
    return ["sh", "gradlew", "assembleRelease", "--no-daemon", "--console=plain"]


def find_release_apks(repo_root: Path) -> list[Path]:
    """All APKs produced by assembleRelease, sorted by name."""
    outputs = repo_root / "android" / "app" / "build" / "outputs" / "apk" / "release"
    if not outputs.is_dir():
        return []
    return sorted(path for path in outputs.glob("*.apk") if path.is_file())


def _alignment_checker() -> Any:
    """Import ``scripts/check_android_alignment.py`` once."""
    global _ALIGNMENT_MODULE
    if _ALIGNMENT_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "check_android_alignment",
            SCRIPTS_DIR / "check_android_alignment.py",
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ReleaseError("cannot load scripts/check_android_alignment.py")
        module = importlib.util.module_from_spec(spec)
        # Register before executing: dataclasses resolve their module through
        # ``sys.modules``.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ALIGNMENT_MODULE = module
    return _ALIGNMENT_MODULE


def check_apk_alignment(apk: Path) -> None:
    """
    Refuse to publish an APK whose native libraries are not 16 KB aligned.

    Android 15+ cannot load a 4 KB-aligned ``.so`` on a 16 KB page-size
    device: the APK installs and then crashes on launch.  A single dependency
    built without ``-Wl,-z,max-page-size=16384`` is enough to cause it, and
    nothing else in the pipeline would notice.
    """
    report = _alignment_checker().check_apk(apk)
    if report.ok:
        print(f"==> {apk.name}: {len(report.libs)} native lib(s) are 16 KB aligned")
        return
    detail = "; ".join(report.errors[:5])
    raise ReleaseError(f"{apk.name} is not 16 KB page-size ready: {detail}")


_ALIGNMENT_MODULE: Any = None


def android_asset_names(version: str, apks: list[Path]) -> list[str]:
    """Release asset names for the APKs, in the same order as *apks*."""
    if len(apks) == 1:
        return [f"{PROJECT_NAME}-{version}-android.apk"]
    return [f"{PROJECT_NAME}-{version}-android-{apk.stem}.apk" for apk in apks]


def _asset_spec(path: Path, name: str) -> AssetSpec:
    """
    Describe one file for upload, refusing anything that cannot be published.

    Symlinks are rejected instead of silently followed: a link in the build
    directory could point at ``~/.ssh`` or a file outside the release.  The
    2 GiB ceiling is GitHub's own limit; checking it before hashing and
    uploading avoids a multi-minute transfer that can only fail.
    """
    if path.is_symlink():
        raise ReleaseError(f"refusing to publish symlink {name!r} ({path})")
    if not path.is_file():
        raise ReleaseError(f"release asset {name!r} is not a regular file: {path}")
    size = path.stat().st_size
    if size > MAX_ASSET_BYTES:
        raise ReleaseError(
            f"release asset {name!r} is {_human_size(size)}, above GitHub's "
            f"{_human_size(MAX_ASSET_BYTES)} per-asset limit"
        )
    return AssetSpec(path, name, _sha256_file(path))


def _ensure_unique_asset_names(assets: list[AssetSpec]) -> None:
    """Refuse a release that would upload two different files under one name."""
    seen: dict[str, Path] = {}
    for asset in assets:
        previous = seen.get(asset.name)
        if previous is not None:
            raise ReleaseError(
                f"duplicate release asset name {asset.name!r}: "
                f"{previous} and {asset.path}"
            )
        seen[asset.name] = asset.path


def installer_name(version: str) -> str:
    """Release asset name of the Inno Setup installer for *version*."""
    return INSTALLER_NAME_TEMPLATE.format(version=version)


def installer_path(repo_root: Path, version: str) -> Path:
    """Where ``build_desktop.py`` leaves the installer (``dist/``, not the zip dir)."""
    return repo_root / "dist" / installer_name(version)


def _desktop_builder() -> Any:
    """Import ``scripts/build_desktop.py`` once (for its ISCC lookup)."""
    global _DESKTOP_MODULE
    if _DESKTOP_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "build_desktop",
            SCRIPTS_DIR / "build_desktop.py",
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ReleaseError("cannot load scripts/build_desktop.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _DESKTOP_MODULE = module
    return _DESKTOP_MODULE


def find_inno_setup() -> str | None:
    """
    Locate the Inno Setup compiler used by ``build_desktop.py``, or ``None``.

    The same private lookup is reused so a release cannot disagree with the
    builder about whether ISCC is available (``which`` plus the installer's
    usual per-machine/per-user locations).
    """
    found: str | None = _desktop_builder()._find_iscc()
    return found


def _portable_assets(assets: list[AssetSpec]) -> list[AssetSpec]:
    """Assets the updater classifies as a portable desktop archive."""
    builder = _manifest_builder()
    return [
        asset
        for asset in assets
        if (builder.classify_asset(asset.name) or "").endswith("-portable")
    ]


def verify_release_requirements(
    assets: list[AssetSpec],
    *,
    targets: list[str],
    version: str,
    skip_installer: bool,
) -> None:
    """
    Enforce the desktop payload contract before ``latest.json`` is written.

    An installed copy updates from ``windows-x64-installer``; when that asset
    is missing it silently falls back to the portable zip, so the release
    looks complete while installed users never receive the new build.  For the
    ``desktop`` target this is therefore a hard error, not a warning:

    * default: the ``NovelDownloader-setup-<version>-x64.exe`` asset must be
      collected (and ISCC must have been available to build it);
    * ``--skip-installer``: the installer is deliberately absent and the
      manifest must not advertise it, so the portable zip is required as the
      fallback.  With neither asset the release is not publishable.

    :raises ReleaseError: when the configured desktop shape is incomplete.
    """
    if "desktop" not in targets:
        return

    portable = _portable_assets(assets)
    if skip_installer:
        if not portable:
            raise ReleaseError(
                "--skip-installer publishes no installer, so the desktop "
                "target still needs the portable zip fallback "
                f"(NovelDownloader-{version}-*.zip); none was collected"
            )
        print(f"==> Installer skipped (--skip-installer); portable: {portable[0].name}")
        return

    expected = installer_name(version)
    if not any(asset.name == expected for asset in assets):
        raise ReleaseError(
            f"the desktop target requires the Inno Setup installer {expected}; "
            "install Inno Setup 6 (ISCC) so build_desktop.py can compile it, "
            "or pass --skip-installer to publish the portable zip only"
        )
    print(f"==> Installer required and present: {expected}")


def collect_content_assets(
    repo_root: Path,
    release_dir: Path,
    version: str,
    *,
    include_apks: bool,
    include_installer: bool = True,
) -> list[AssetSpec]:
    """
    The release's "content" assets: bundle zips, (optional) APK, installer.

    ``SHA256SUMS.txt`` and ``manifest.json`` are metadata produced alongside
    the content and are added later, after the checksums are merged.  APKs are
    only included when the Android build ran in this release (or, for the
    dry-run preview, when a signed output is already on disk).  The installer
    is included only when the desktop target asked for it: ``--skip-installer``
    must not upload a stale exe that ``latest.json`` will not advertise.
    """
    metadata = {"SHA256SUMS.txt", "manifest.json", "latest.json"}
    files = sorted(
        path
        for path in release_dir.iterdir()
        if (path.is_file() or path.is_symlink()) and path.name not in metadata
    )
    assets = [_asset_spec(path, path.name) for path in files]

    if include_apks:
        apks = find_release_apks(repo_root)
        names = android_asset_names(version, apks)
        assets.extend(
            _asset_spec(apk, name) for apk, name in zip(apks, names, strict=True)
        )

    installer = installer_path(repo_root, version)
    if include_installer and (installer.is_file() or installer.is_symlink()):
        assets.append(_asset_spec(installer, installer.name))
    _ensure_unique_asset_names(assets)
    return assets


def _manifest_builder() -> Any:
    """Import ``scripts/build_update_manifest.py`` once."""
    global _MANIFEST_MODULE
    if _MANIFEST_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "build_update_manifest",
            SCRIPTS_DIR / "build_update_manifest.py",
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ReleaseError("cannot load scripts/build_update_manifest.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _MANIFEST_MODULE = module
    return _MANIFEST_MODULE


def _manifest_payload(assets: list[AssetSpec], tag: str, slug: str) -> dict[str, Any]:
    """The release-shaped payload ``build_update_manifest`` classifies."""
    builder = _manifest_builder()
    return {
        "html_url": f"https://github.com/{slug}/releases/tag/{tag}",
        "assets": [
            {
                "name": asset.name,
                "browser_download_url": builder.expected_url(slug, tag, asset.name),
                "digest": f"sha256:{asset.sha256}",
                "size": asset.path.stat().st_size,
            }
            for asset in assets
            if asset.name != "latest.json"
        ],
    }


def validate_latest_manifest(
    document: dict[str, Any],
    assets: list[AssetSpec],
    *,
    version: str,
    tag: str,
    slug: str,
) -> None:
    """
    Cross-check ``latest.json`` against the files about to be uploaded.

    ``build_update_manifest`` trusts the payload it is given; here the payload
    is written by this script, so the manifest must agree with the bytes on
    disk in three ways before it is published:

    * identity: the document is for this version, tag and repository;
    * integrity: every advertised ``sha256``/``size`` is the local file's;
    * coverage: every content asset is advertised, and nothing else is.

    A mismatch means clients would download the wrong file, or never be
    offered an update at all, so this raises :class:`ReleaseError`.
    :raises ReleaseError: when the document and the assets disagree.
    """
    builder = _manifest_builder()
    for field, expected in (("version", version), ("tag", tag), ("repo", slug)):
        if document.get(field) != expected:
            raise ReleaseError(
                f"latest.json {field} is {document.get(field)!r}, expected {expected!r}"
            )

    platforms = document.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ReleaseError("latest.json advertises no platforms")

    by_name = {asset.name: asset for asset in assets if asset.name != "latest.json"}
    advertised: set[str] = set()
    for key in sorted(platforms):
        entry = platforms[key]
        if not isinstance(entry, dict):
            raise ReleaseError(f"latest.json platform {key!r} is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or name not in by_name:
            raise ReleaseError(
                f"latest.json platform {key!r} advertises unknown asset {name!r}"
            )
        # Re-derive the key from the name: a manifest that says
        # ``windows-x64-installer`` but points at the portable zip would send
        # an installed copy the wrong package.  Both sides use the same rule.
        derived = builder.classify_asset(name)
        if derived != key:
            raise ReleaseError(
                f"latest.json platform {key!r} points at {name!r}, which "
                f"classifies as {derived or 'a non-updatable asset'}"
            )
        # A stale file left in the release directory (or an installer from
        # another version) must never be advertised as this release.
        if f"-{version}-" not in name:
            raise ReleaseError(
                f"latest.json platform {key!r} points at {name!r}, which does "
                f"not carry the release version {version!r}"
            )
        asset = by_name[name]
        want_url = builder.expected_url(slug, tag, name)
        if entry.get("url") != want_url:
            raise ReleaseError(
                f"latest.json platform {key!r} points at {entry.get('url')!r}, "
                f"expected {want_url!r}"
            )
        digest = str(entry.get("sha256") or "").lower()
        if digest != asset.sha256.lower():
            raise ReleaseError(
                f"latest.json platform {key!r} advertises sha256 {digest or '<none>'}, "
                f"but {name} hashes to {asset.sha256}"
            )
        if entry.get("size") != asset.path.stat().st_size:
            raise ReleaseError(
                f"latest.json platform {key!r} advertises size "
                f"{entry.get('size')!r}, but {name} is "
                f"{asset.path.stat().st_size} bytes"
            )
        advertised.add(name)

    missing = sorted(name for name in by_name if name not in advertised)
    if missing:
        raise ReleaseError("latest.json would not advertise: " + ", ".join(missing))


def build_manifest_document(
    assets: list[AssetSpec], version: str, tag: str, slug: str
) -> dict[str, Any]:
    """
    Build the ``latest.json`` document in memory (no file, no network).

    Used both by the real release (before writing) and by ``--dry-run``,
    which prints the platforms it would advertise without touching the
    release directory.
    """
    builder = _manifest_builder()
    payload = _manifest_payload(assets, tag, slug)
    try:
        document: dict[str, Any] = builder.build_manifest(
            payload,
            version=version,
            tag=tag,
            repo=slug,
            notes_url=payload["html_url"],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a release error
        raise ReleaseError(f"cannot build latest.json: {exc}") from exc
    # Every advertised URL must point at the tag being released: a manifest
    # carrying another release's URLs would send clients to the wrong file.
    validate_latest_manifest(document, assets, version=version, tag=tag, slug=slug)
    return document


def build_latest_manifest(
    assets: list[AssetSpec],
    release_dir: Path,
    version: str,
    tag: str,
    slug: str,
    *,
    sign_key: Path | None = None,
) -> AssetSpec:
    """
    Write ``latest.json`` describing *assets* and return it as an asset.

    The document is derived from the files about to be uploaded, never from
    build-time state: if a bundle was not built, its platform simply has no
    entry, instead of pointing at last release's file.  The bytes written are
    re-parsed and re-validated so a serialisation bug cannot publish a
    manifest different from the one checked above.
    """
    builder = _manifest_builder()
    document = build_manifest_document(assets, version, tag, slug)
    if sign_key is not None:
        try:
            seed = sign_key.read_text(encoding="ascii").strip()
            document = builder.sign_document(document, seed)
        except Exception as exc:  # noqa: BLE001 - any failure aborts
            raise ReleaseError(f"cannot sign latest.json: {exc}") from exc
        print(f"==> latest.json signed (key id {document.get('key_id')})")

    target = release_dir / "latest.json"
    target.write_bytes(builder.manifest_bytes(document))
    try:
        written = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:  # pragma: no cover - canonical JSON cannot fail
        raise ReleaseError(
            f"latest.json is not valid JSON after writing: {exc}"
        ) from exc
    validate_latest_manifest(written, assets, version=version, tag=tag, slug=slug)
    print(f"==> latest.json: {len(document['platforms'])} platform(s) for v{version}")
    return AssetSpec(target, target.name, _sha256_file(target))


_MANIFEST_MODULE: Any = None
_DESKTOP_MODULE: Any = None


def _audit_module() -> Any:
    """Import ``scripts/audit_release_assets.py`` once."""
    global _AUDIT_MODULE
    if _AUDIT_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "audit_release_assets",
            SCRIPTS_DIR / "audit_release_assets.py",
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ReleaseError("cannot load scripts/audit_release_assets.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _AUDIT_MODULE = module
    return _AUDIT_MODULE


def audit_release_content(assets: list[AssetSpec]) -> None:
    """
    Refuse to publish assets that leak private data or hide files.

    A PyInstaller bundle can pull in a stray ``.pdb``, a ``.env`` from the
    build directory or a token embedded in a config sample.  Nothing else in
    the pipeline inspects the bytes, so this is a hard gate before upload.
    """
    result = _audit_module().audit_files([(asset.name, asset.path) for asset in assets])
    if result.problems:
        detail = "; ".join(
            f"{problem.asset}: {problem.kind} ({problem.detail})"
            for problem in result.problems[:5]
        )
        raise ReleaseError(
            f"release asset audit failed ({len(result.problems)} problem(s)): {detail}"
        )
    print(
        f"==> asset audit clean: {result.scanned} file(s), "
        f"{result.python_files} bundled .py file(s)"
    )


_AUDIT_MODULE: Any = None


def merge_sums(sums_path: Path, entries: list[tuple[str, str]]) -> list[str]:
    """
    Merge ``(asset_name, sha256)`` pairs into ``SHA256SUMS.txt``.

    Existing lines keep their order and are updated in place when a digest
    changes; new names are appended.  Running it twice with the same input
    produces the identical file.  Returns the final lines.
    """
    order: list[str] = []
    digests: dict[str, str] = {}
    if sums_path.is_file():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            match = _SUMS_LINE_RE.match(line.strip())
            if match is None:
                continue
            name = match["name"].strip()
            if name not in digests:
                order.append(name)
            digests[name] = match["sha"].lower()
    for name, sha in entries:
        if name not in digests:
            order.append(name)
        digests[name] = sha.lower()
    lines = [f"{digests[name]}  {name}" for name in order]
    text = ("\n".join(lines) + "\n") if lines else ""
    sums_path.parent.mkdir(parents=True, exist_ok=True)
    sums_path.write_text(text, encoding="utf-8", newline="\n")
    return lines


def parse_sums(sums_path: Path) -> dict[str, str]:
    """
    Parse ``SHA256SUMS.txt`` into ``{asset_name: sha256}``.

    Blank lines and anything that is not the conventional
    ``<64 hex>  <name>`` line are ignored, so a hand-edited header does not
    turn into a bogus asset.
    """
    digests: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        match = _SUMS_LINE_RE.match(line.strip())
        if match is None:
            continue
        digests[match["name"].strip()] = match["sha"].lower()
    return digests


def validate_sums_against_manifest(sums_path: Path, manifest_path: Path) -> None:
    """
    Assert ``SHA256SUMS.txt`` and ``latest.json`` describe the same bytes.

    Both files are published side by side and a client may verify a download
    against either one.  If they disagree, one of them is stale: the updater
    would accept a file the checksum list rejects (or vice versa), and which
    one is right is impossible to tell after the fact.  So this is a hard
    gate, run after the manifest is written and the checksums are merged.

    ``latest.json`` itself must be listed too: it is the document clients
    trust before downloading anything, so its own digest has to be
    verifiable from the same checksum file.

    :raises ReleaseError: when a platform is missing, a digest differs, or
        the manifest's own checksum does not match the file on disk.
    """
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {manifest_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReleaseError(f"{manifest_path}: expected a JSON object")

    sums = parse_sums(sums_path)
    problems: list[str] = []
    platforms = document.get("platforms")
    if not isinstance(platforms, dict):
        raise ReleaseError(f"{manifest_path}: platforms is not an object")
    for key in sorted(platforms):
        entry = platforms[key]
        if not isinstance(entry, dict):
            raise ReleaseError(f"{manifest_path}: platform {key!r} is not an object")
        name = entry.get("name")
        digest = str(entry.get("sha256") or "").lower()
        listed = sums.get(str(name))
        if listed is None:
            problems.append(f"{name} is missing from SHA256SUMS.txt")
        elif listed != digest:
            problems.append(
                f"{name} is {listed} in SHA256SUMS.txt but {digest} in latest.json"
            )

    manifest_digest = _sha256_file(manifest_path)
    listed_manifest = sums.get(manifest_path.name)
    if listed_manifest is None:
        problems.append("latest.json is missing from SHA256SUMS.txt")
    elif listed_manifest != manifest_digest:
        problems.append(
            f"latest.json is {listed_manifest} in SHA256SUMS.txt but hashes "
            f"to {manifest_digest}"
        )

    if problems:
        raise ReleaseError(
            "SHA256SUMS.txt and latest.json disagree: " + "; ".join(problems)
        )
    print(f"==> SHA256SUMS.txt agrees with latest.json ({len(platforms)} platform(s))")


# ---------------------------------------------------------------------------
# GitHub credentials
# ---------------------------------------------------------------------------


def github_token(repo_root: Path) -> str | None:
    """
    Return a GitHub token, or ``None`` when no source is available.

    Order: ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment (how CI provides the
    workflow token), then ``git credential fill`` for protocol=https /
    host=github.com, which consults the configured credential helper (e.g.
    Git Credential Manager) without ever storing or printing the secret.
    """
    for variable in TOKEN_ENV_VARS:
        token = os.environ.get(variable)
        if token and token.strip():
            return token.strip()
    try:
        proc = _run(
            ["git", "credential", "fill"],
            cwd=repo_root,
            input_text=f"protocol=https\nhost={GITHUB_HOST}\n\n",
            timeout=CREDENTIAL_TIMEOUT,
            check=False,
            # Never block on an interactive prompt: fail instead.
            extra_env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
    except ReleaseError as exc:
        print(f"warning: git credential fill failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            secret = line[len("password=") :].strip()
            return secret or None
    return None


# ---------------------------------------------------------------------------
# GitHub REST client (tests monkeypatch urllib.request.urlopen)
# ---------------------------------------------------------------------------


def _parse_json(raw: bytes) -> Any:
    """Best-effort JSON parse of an API body; ``None`` when not JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


class GitHubClient:
    """The small slice of the GitHub REST API the release needs."""

    def __init__(self, slug: str, token: str, api_base: str = GITHUB_API) -> None:
        self._slug = slug.rstrip("/")
        self._token = token
        self._api = api_base.rstrip("/")

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> Reply:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{PROJECT_NAME}-release-script",
        }
        body: bytes | None = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout if timeout is not None else HTTP_TIMEOUT
            ) as response:
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise TransientAPIError(
                f"network error calling {method} {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise TransientAPIError(
                f"network timeout calling {method} {url}: {exc}"
            ) from exc
        payload = _parse_json(raw)
        if status not in expected:
            message = (
                payload.get("message") if isinstance(payload, dict) else None
            ) or raw[:200].decode("utf-8", "replace")
            # GitHub puts the machine-readable reason (e.g. ``already_exists``)
            # in ``errors``; keeping it lets the upload path tell a lost
            # response apart from a genuinely invalid asset.
            if isinstance(payload, dict) and payload.get("errors"):
                message = (
                    f"{message} "
                    + json.dumps(payload["errors"], ensure_ascii=False)[:300]
                )
            # GitHub reports exhausted primary and secondary rate limits as
            # 403 (not 429); those are worth retrying, like a 5xx.
            rate_limited = (
                status == 403
                and isinstance(payload, dict)
                and (
                    "rate limit" in str(payload.get("message") or "").lower()
                    or str(payload.get("documentation_url") or "").endswith(
                        "rate-limiting"
                    )
                )
            )
            error = (
                TransientAPIError
                if status in {429, 500, 502, 503, 504} or rate_limited
                else ReleaseError
            )
            raise error(f"GitHub API {method} {url} returned HTTP {status}: {message}")
        return Reply(status, payload)

    def _json(self, reply: Reply, context: str) -> dict[str, Any]:
        if not isinstance(reply.payload, dict):
            raise ReleaseError(f"{context}: unexpected API body: {reply.payload!r}")
        return reply.payload

    # -- endpoints ---------------------------------------------------------

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        """
        The **published** release at *tag*, or ``None`` when there is none.

        GitHub's tag lookup deliberately hides drafts, so a draft created by an
        interrupted run is found with :meth:`find_draft_release` instead.
        """
        url = f"{self._api}/repos/{self._slug}/releases/tags/{quote(tag)}"
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        return self._json(reply, f"release lookup for {tag}")

    def get_release_by_id(self, release_id: int) -> dict[str, Any] | None:
        """One release by numeric id; drafts are visible to a write token."""
        url = f"{self._api}/repos/{self._slug}/releases/{release_id}"
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        return self._json(reply, f"release {release_id}")

    def find_draft_release(self, tag: str) -> dict[str, Any] | None:
        """
        The draft release for *tag*, if an earlier run left one behind.

        ``releases/tags/<tag>`` only returns published releases.  The list
        endpoint does include drafts for a token with push access; it is paged
        newest-first, so a run that died a few pages back is still found.
        """
        for page in range(1, RELEASE_LIST_PAGES + 1):
            url = (
                f"{self._api}/repos/{self._slug}/releases"
                f"?per_page={RELEASE_LIST_PAGE_SIZE}&page={page}"
            )
            reply = self._request("GET", url)
            items = reply.payload
            if not isinstance(items, list):
                raise ReleaseError(f"release list page {page}: unexpected API body")
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("draft") is True
                    and item.get("tag_name") == tag
                ):
                    return item
            if len(items) < RELEASE_LIST_PAGE_SIZE:
                break
        return None

    def get_tag_ref_sha(self, tag: str) -> str | None:
        """The commit a remote tag ref points at, or ``None`` when absent."""
        url = f"{self._api}/repos/{self._slug}/git/ref/tags/{quote(tag)}"
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        payload = self._json(reply, f"tag ref {tag}")
        target = payload.get("object")
        if isinstance(target, dict) and isinstance(target.get("sha"), str):
            return str(target["sha"])
        return None

    def ensure_tag_ref(self, tag: str, sha: str) -> None:
        """
        Point ``refs/tags/<tag>`` at *sha*, tolerating an identical existing ref.

        Used as the fallback when ``git push`` cannot reach github.com (the
        git endpoint and the REST host are separate services, and only the
        latter may be reachable).  A 422 saying the ref already exists is only
        success when it points at *sha*: a tag left over from an earlier
        attempt at a different commit would publish this build's assets under
        another commit, so that is a hard error.
        """
        url = f"{self._api}/repos/{self._slug}/git/refs"
        reply = self._request(
            "POST",
            url,
            json_body={"ref": f"refs/tags/{tag}", "sha": sha},
            expected=(201, 422),
        )
        if reply.status == 422:
            message = ""
            if isinstance(reply.payload, dict):
                message = str(reply.payload.get("message") or "")
            if "already exists" not in message.lower():
                raise ReleaseError(f"could not create tag ref {tag}: {message}")
            existing = self.get_tag_ref_sha(tag)
            if existing is not None and existing != sha:
                raise ReleaseError(
                    f"tag {tag} already exists on the remote at {existing[:12]}, "
                    f"but this release needs {sha[:12]}; delete or move the tag "
                    "before releasing"
                )

    def create_release(
        self,
        tag: str,
        name: str,
        body: str,
        *,
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict[str, Any]:
        """Create (and publish) a release for *tag*."""
        url = f"{self._api}/repos/{self._slug}/releases"
        reply = self._request(
            "POST",
            url,
            json_body={
                "tag_name": tag,
                "name": name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
            },
            expected=(200, 201),
        )
        return self._json(reply, "release creation")

    def update_release(self, release_id: int, body: str) -> dict[str, Any]:
        """Replace the notes of an existing release."""
        url = f"{self._api}/repos/{self._slug}/releases/{release_id}"
        reply = self._request("PATCH", url, json_body={"body": body})
        return self._json(reply, f"release {release_id} update")

    def publish_release(self, release_id: int) -> dict[str, Any]:
        """Flip a draft release to published once every asset has verified."""
        url = f"{self._api}/repos/{self._slug}/releases/{release_id}"
        reply = self._request("PATCH", url, json_body={"draft": False})
        return self._json(reply, f"release {release_id} publish")

    def delete_asset(self, asset_id: int) -> None:
        """Delete one release asset by id."""
        url = f"{self._api}/repos/{self._slug}/releases/assets/{asset_id}"
        self._request("DELETE", url, expected=(200, 204))

    def _asset_already_uploaded(self, upload_url: str, asset: AssetSpec) -> bool:
        """
        Whether a 422 ``already_exists`` upload actually landed with our bytes.

        A response can be lost after GitHub committed the upload (a write
        timeout, a dropped connection).  Retrying then fails with 422 even
        though the release is fine.  Looking the asset up by name and
        comparing digests distinguishes that from a genuine name conflict.
        """
        match = re.search(r"/releases/(\d+)/assets/?$", upload_url)
        if match is None:
            return False
        url = (
            f"{self._api}/repos/{self._slug}/releases/{match.group(1)}/assets"
            f"?name={quote(asset.name)}"
        )
        try:
            reply = self._request("GET", url, expected=(200,))
        except ReleaseError:
            return False
        if not isinstance(reply.payload, list):
            return False
        for item in reply.payload:
            if isinstance(item, dict) and item.get("name") == asset.name:
                return _remote_digest(item) == asset.sha256.lower()
        return False

    def upload_asset(self, upload_url: str, asset: AssetSpec) -> None:
        """
        Upload *asset* to the release's upload endpoint, retrying transient faults.

        A 55 MiB upload over a slow link fails regularly with a write timeout;
        aborting the whole release for that wastes the build and leaves the
        release half-populated.  GitHub only registers an asset once the upload
        completes, so retrying the same name is safe — unless the timed-out
        attempt did complete, in which case GitHub answers ``already_exists``;
        that is success when the remote digest matches ours.
        """
        base = upload_url.split("{", 1)[0].rstrip("/")
        if not base:
            raise ReleaseError(
                "release payload has no upload_url; cannot upload assets"
            )
        url = f"{base}?name={quote(asset.name)}"
        payload = asset.path.read_bytes()
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                self._request(
                    "POST",
                    url,
                    data=payload,
                    content_type="application/octet-stream",
                    expected=(200, 201),
                    timeout=UPLOAD_TIMEOUT,
                )
            except TransientAPIError as exc:
                if attempt == UPLOAD_ATTEMPTS - 1:
                    raise
                delay = UPLOAD_BACKOFF[min(attempt, len(UPLOAD_BACKOFF) - 1)]
                print(
                    f"  asset {asset.name}: {exc}; retrying in {delay:.0f}s "
                    f"({attempt + 1}/{UPLOAD_ATTEMPTS - 1})"
                )
                time.sleep(delay)
                continue
            except ReleaseError as exc:
                if "already_exists" in str(
                    exc
                ).lower() and self._asset_already_uploaded(base, asset):
                    print(f"  asset {asset.name}: already present, skipping")
                    return
                raise
            print(
                f"  asset {asset.name}: uploaded "
                f"({_human_size(asset.path.stat().st_size)})"
            )
            return


def _remote_digest(asset: dict[str, Any]) -> str | None:
    """The ``sha256:<hex>`` digest of a remote asset, hex part only."""
    value = asset.get("digest")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value or None


def sync_assets(
    client: GitHubClient, release: dict[str, Any], assets: list[AssetSpec]
) -> None:
    """
    Upload *assets* to *release*, replacing anything whose content changed.

    Idempotent: an asset whose remote digest equals the local SHA-256 is
    skipped; an asset with the same name but different content is deleted and
    re-uploaded; a missing asset is simply uploaded.

    Assets that are on the release but not in *assets* are **kept** and
    reported: deleting published files automatically is destructive, and a
    stale file is usually harmless (the manifest simply no longer points at
    it).  Remove them by hand if the release page should be tidy.
    """
    remote: dict[str, dict[str, Any]] = {}
    for item in release.get("assets") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            remote[str(item["name"])] = item

    for asset in assets:
        existing = remote.get(asset.name)
        if existing is not None:
            remote_digest = _remote_digest(existing)
            if remote_digest == asset.sha256:
                print(f"  asset {asset.name}: up to date, skipping")
                continue
            if remote_digest is None:
                # GitHub only reports a digest for assets uploaded after the
                # feature shipped; without one the bytes cannot be compared,
                # so the only safe action is a replace.
                print(f"  asset {asset.name}: no remote digest, replacing to be safe")
            else:
                print(f"  asset {asset.name}: digest differs, replacing")
            try:
                asset_id = int(existing["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReleaseError(
                    f"release asset {asset.name!r} has no usable id; cannot "
                    "replace it through the API"
                ) from exc
            client.delete_asset(asset_id)
        client.upload_asset(str(release.get("upload_url", "")), asset)

    intended = {asset.name for asset in assets}
    extras = sorted(name for name in remote if name not in intended)
    if extras:
        print(
            "warning: the release has extra asset(s) not in this build: "
            + ", ".join(extras)
        )


def verify_assets(
    client: GitHubClient,
    tag: str,
    assets: list[AssetSpec],
    *,
    release_id: int | None = None,
) -> None:
    """
    Re-read the release and assert every intended asset is actually present.

    Uploading is the one step whose failure used to be invisible: a release
    published by a concurrent run (or a partially failed upload) left the
    release looking complete while a bundle or the APK was missing.  Checking
    the final state costs one request and turns that into a hard error.

    *release_id* selects the by-id endpoint, which (unlike the tag lookup) also
    returns draft releases; the normal release flow verifies a draft before it
    is published.
    """
    current = (
        client.get_release_by_id(release_id)
        if release_id is not None
        else client.get_release_by_tag(tag)
    )
    if current is None:
        raise ReleaseError(f"release {tag} vanished right after publishing")

    present = {
        str(item["name"])
        for item in current.get("assets") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    missing = sorted(asset.name for asset in assets if asset.name not in present)
    if missing:
        raise ReleaseError(
            f"release {tag} is missing {len(missing)} asset(s): " + ", ".join(missing)
        )

    mismatched = [
        asset.name
        for asset in assets
        if (remote_digest := _remote_digest_by_name(current, asset.name)) is not None
        and remote_digest != asset.sha256
    ]
    # The manifest decides where every client downloads from, so it has to be
    # verifiable: an unverifiable entry means we cannot prove the published
    # URLs are the ones we wrote.
    publishes_manifest = any(asset.name == "latest.json" for asset in assets)
    if publishes_manifest and _remote_digest_by_name(current, "latest.json") is None:
        raise ReleaseError(
            "release has no digest for latest.json; refusing to leave the "
            "update manifest unverified"
        )
    if mismatched:
        raise ReleaseError(
            f"release {tag} has stale content for: " + ", ".join(sorted(mismatched))
        )
    print(f"==> Verified {len(assets)} asset(s) on release {tag}")


def _remote_digest_by_name(release: dict[str, Any], name: str) -> str | None:
    """The remote digest of the asset called *name*, if GitHub reports one."""
    for item in release.get("assets") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return _remote_digest(item)
    return None


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def _git_stdout(
    repo_root: Path, args: list[str], *, timeout: float | None = None
) -> str | None:
    """Run a read-only git command; its stripped stdout, or ``None`` on failure."""
    proc = _run(["git", *args], cwd=repo_root, timeout=timeout, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def local_tag_commit(repo_root: Path, tag: str) -> str | None:
    """The commit a local tag points at, or ``None`` when the tag is absent."""
    return _git_stdout(
        repo_root, ["rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"]
    )


def head_commit(repo_root: Path) -> str | None:
    """The current ``HEAD`` commit, or ``None`` outside a git checkout."""
    return _git_stdout(repo_root, ["rev-parse", "HEAD"])


def tag_exists_local(repo_root: Path, tag: str) -> bool:
    """Whether *tag* already exists in the local repository."""
    return local_tag_commit(repo_root, tag) is not None


def print_push_api_hint() -> None:
    """
    Point at ``scripts/push_via_api.py`` when the git endpoint is blocked.

    Only a hint: the helper rewrites a branch ref, so it stays a deliberate
    manual step instead of something a release run does on its own.
    """
    print(PUSH_API_HINT, file=sys.stderr)
    print(
        "      dry run first: python scripts/push_via_api.py --dry-run",
        file=sys.stderr,
    )


def remote_tag_commit(repo_root: Path, tag: str) -> str | None:
    """
    The commit *tag* points at on origin, or ``None`` when it is not there.

    ``github.com`` being unreachable must not abort the release: the query is
    bounded by :data:`GIT_REMOTE_TIMEOUT`, and any failure is reported and
    treated as "not there yet".  The subsequent push then fails over to the
    REST API (see :func:`push_tag`), which may still be reachable.

    For an annotated tag ``ls-remote`` lists both the tag object and the
    peeled ``^{}`` commit; the peeled commit is what the local tag resolves to
    and is preferred, so the two sides are comparable.
    """
    try:
        proc = _run(
            ["git", "ls-remote", "origin", f"refs/tags/{tag}"],
            cwd=repo_root,
            timeout=GIT_REMOTE_TIMEOUT,
            check=False,
        )
    except ReleaseError as exc:
        print(
            f"warning: could not query origin for tag {tag} ({exc}); "
            "assuming it is not pushed yet",
            file=sys.stderr,
        )
        print_push_api_hint()
        return None
    if proc.returncode != 0:
        print(
            f"warning: could not query origin for tag {tag}; "
            "assuming it is not pushed yet",
            file=sys.stderr,
        )
        return None
    plain: str | None = None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sha, ref = parts[0], parts[1]
        if ref == f"refs/tags/{tag}^{{}}":
            return sha
        if ref == f"refs/tags/{tag}":
            plain = sha
    return plain


def remote_tag_exists(repo_root: Path, tag: str) -> bool:
    """Whether *tag* is already on the origin remote (one network query)."""
    return remote_tag_commit(repo_root, tag) is not None


def verify_tag_target(
    tag: str,
    *,
    local_sha: str | None,
    head_sha: str | None,
    remote_sha: str | None,
) -> None:
    """
    Refuse to publish under a tag that points at another commit.

    Assets are built from ``HEAD`` and are served under the tag's commit.
    A tag left over from an earlier attempt (or pushed by someone else) at a
    different commit would attach this build to the wrong source; git cannot
    fix that without rewriting published history, so this is a hard error.
    """
    if local_sha is not None and head_sha is not None and local_sha != head_sha:
        raise ReleaseError(
            f"tag {tag} already exists locally at {local_sha[:12]}, but HEAD is "
            f"{head_sha[:12]}; delete or move the tag before releasing"
        )
    expected = local_sha or head_sha
    if remote_sha is not None and expected is not None and remote_sha != expected:
        raise ReleaseError(
            f"tag {tag} already exists on origin at {remote_sha[:12]}, but "
            f"this release needs {expected[:12]}; delete or move the remote tag "
            "before releasing"
        )


def create_tag(repo_root: Path, tag: str) -> None:
    """
    Create *tag* (lightweight, like the repository's existing v1.1.0/v1.2.0,
    and independent of any git user identity being configured).
    """
    _announce(["git", "tag", tag])
    _run(["git", "tag", tag], cwd=repo_root)


def _git_auth_env(token: str) -> dict[str, str]:
    """
    Environment that authenticates one ``git`` process without exposing the token.

    A token in the remote URL would show up in ``argv`` (and therefore in the
    Windows process list and any wrapper that logs its command line) and in
    git's own error output.  ``http.<url>.extraheader`` via ``GIT_CONFIG_*``
    keeps the credential in the child's environment; the parent's command line
    stays token-free.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{GITHUB_HOST}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: basic {encoded}",
    }


def push_tag(
    repo_root: Path,
    tag: str,
    token: str | None,
    slug: str,
    *,
    api_base: str = GITHUB_API,
) -> None:
    """
    Push *tag* to origin.

    With an environment token (CI) the push authenticates through a per-process
    git config header; the token is never part of the command line.  Without
    one, plain ``git push`` lets git resolve credentials through its own
    credential helper chain (the same store ``git credential fill`` reads).

    ``github.com:443`` and ``api.github.com`` are different services: when
    only the latter is reachable, the tag ref is created through the REST API
    instead of failing the whole release after a successful build.  The
    ``api_base`` override is honoured so a dry run / GitHub Enterprise setup
    stays consistent with the rest of the release.
    """
    command: list[str] = ["git", "push", "origin", tag]
    secrets: tuple[str, ...] = (token,) if token else ()
    extra_env = _git_auth_env(token) if token else None
    _announce(command)
    try:
        _run(
            command,
            cwd=repo_root,
            timeout=PUSH_TIMEOUT,
            extra_env=extra_env,
            secrets=secrets,
        )
        return
    except ReleaseError as exc:
        print(f"git push failed ({exc})", file=sys.stderr)
        print_push_api_hint()
        if not token:
            raise
        print("creating the tag ref through the REST API")
        # The ref must point at the tag's own commit: HEAD may be ahead of it
        # (a docs commit after the build), and GitHub rejects unknown objects.
        target = _run(
            ["git", "rev-parse", f"{tag}^{{commit}}"], cwd=repo_root
        ).stdout.strip()
        if not target:
            raise ReleaseError(
                f"cannot resolve {tag} to a commit; refusing the REST fallback"
            ) from exc
        try:
            GitHubClient(slug, token, api_base=api_base).ensure_tag_ref(tag, target)
        except ReleaseError as api_exc:
            raise ReleaseError(
                f"tag {tag} could not be pushed with git ({exc}) nor created "
                f"through the REST API ({api_exc})"
            ) from api_exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_targets(value: str) -> list[str]:
    """Parse a comma-separated target list ('all' selects every target)."""
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not items or items == ["all"]:
        return list(DEFAULT_TARGETS)
    unknown = [item for item in items if item not in DEFAULT_TARGETS]
    if unknown:
        raise ReleaseError(
            f"unknown target(s): {', '.join(unknown)} "
            f"(choose from {', '.join(DEFAULT_TARGETS)} or 'all')"
        )
    return list(dict.fromkeys(items))


def _build_command(targets: list[str]) -> list[str]:
    """The build_release.py invocation for *targets*."""
    return [
        sys.executable,
        str(SCRIPTS_DIR / "build_release.py"),
        "--targets",
        ",".join(targets),
    ]


def _print_notes_preview(notes: str, version: str) -> None:
    print(f"Release notes (CHANGELOG.md, section {version}):")
    for line in notes.splitlines():
        print(f"  {line}" if line else "")
    print()


def _print_preflight(
    repo_root: Path, *, version: str, tag: str, skip_secret_scan: bool
) -> None:
    """
    Show what was validated before any build, and what remains for the real run.

    ``--dry-run`` executes no subprocess and does no network I/O, so the
    clean-worktree and tag/release-conflict checks cannot run here; they are
    listed instead of being silently omitted.  The secret scan is listed for
    the same reason: a dry run must not shell out to ``git``.
    """
    gradle = repo_root / "android" / "app" / "build.gradle.kts"
    print("Preflight (validated before any build):")
    print(f"  [ok] __version__ == {version}")
    if gradle.is_file():
        print(f"  [ok] android versionName == {version}")
    else:
        print("  [ok] android versionName: skipped (no android/app/build.gradle.kts)")
    print(f"  [ok] CHANGELOG.md top section `## v{version} (` is non-empty")
    print(f"  [ok] tag {tag} matches version {version}")
    if skip_secret_scan:
        print("  [skip] tracked-file secret scan (--skip-secret-scan; NOT recommended)")
    else:
        print(
            "  [run] tracked-file secret scan before the build "
            "(scripts/scan_secrets.py)"
        )
    print("  [release] clean working tree (git status --porcelain)")
    print("  [release] tag/release conflicts (local tag, origin tag, GitHub release)")
    print()


def _print_asset_requirements(
    *,
    targets: list[str],
    version: str,
    skip_installer: bool,
    assets: list[AssetSpec] | None,
) -> None:
    """Show the desktop payload the configured release requires (dry run)."""
    if "desktop" not in targets:
        print("      required: none (desktop target not selected)")
        return
    if skip_installer:
        print(
            "      required: portable desktop zip "
            "(installer omitted by --skip-installer)"
        )
        if assets is not None and not _portable_assets(assets):
            print(
                "      WARNING: no portable desktop zip collected; the real run fails"
            )
        return
    name = installer_name(version)
    print(f"      required: installer {name} (Inno Setup)")
    if assets is not None and not any(asset.name == name for asset in assets):
        print(
            "      WARNING: installer missing; the real run fails "
            "(install ISCC or pass --skip-installer)"
        )


def _dry_run(
    args: argparse.Namespace,
    *,
    repo_root: Path,
    version: str,
    tag: str,
    targets: list[str],
    notes: str,
) -> int:
    """Print the full plan; touch nothing (no subprocess, no network)."""
    release_dir = find_release_dir(repo_root, version)
    signing_ok, signing_reason = android_signing_available(repo_root)
    build_cmd = _build_command(targets)

    print("DRY RUN — nothing below is executed (no builds, git or API calls).")
    print(f"Version     : {version}")
    print(f"Tag         : {tag}")
    print(f"Repository  : {args.repo or '(from git remote origin)'}")
    print(f"Targets     : {', '.join(targets)}")
    print(f"Auth        : {TOKEN_ENV_VARS[0]}/GH_TOKEN env, else git credential fill")
    print()
    _print_preflight(
        repo_root,
        version=version,
        tag=tag,
        skip_secret_scan=args.skip_secret_scan,
    )
    _print_notes_preview(notes, version)

    print("Plan:")
    if args.skip_build:
        print("  1. Build    : skipped (--skip-build)")
        print(f"              release dir: {release_dir or 'NOT FOUND (build first)'}")
        if release_dir is not None:
            try:
                verify_release_dir(release_dir, version, targets)
            except ReleaseError as exc:
                # A dry run still has no side effects; it just refuses to call
                # an incomplete directory deployable.
                print(f"              WARNING: {exc}")
    else:
        print(f"  1. Build    : {' '.join(build_cmd)}")
    if args.skip_android:
        print("  2. Android  : skipped (--skip-android)")
    elif signing_ok:
        gradle_cmd = " ".join(gradle_command(repo_root))
        print(f"  2. Android  : {gradle_cmd}  (cwd: android)")
    else:
        print(f"  2. Android  : SKIP — {signing_reason}")
    sums = release_dir / "SHA256SUMS.txt" if release_dir else "(not built yet)"
    print(f"  3. Sums     : merge APK/installer checksums into {sums}")
    print(f"  4. Tag      : git tag {tag} (skipped if it already exists)")
    print(
        f"  5. Push     : git push origin {tag} (skipped if already on the "
        "remote; falls back to the REST tag ref when github.com is unreachable)"
    )
    print(
        "              if github.com:443 is blocked: "
        "python scripts/push_via_api.py --dry-run"
    )
    flags = ""
    if args.draft:
        flags += ", draft"
    if args.prerelease:
        flags += ", prerelease"
    print(f"  6. Release  : create {tag} on GitHub with the notes above{flags}")
    if not args.draft:
        print("              as a draft, published only after every asset verifies")
    if release_dir is not None:
        # Mirror the real run: an APK is uploaded only when Android actually
        # builds, so a preview must not advertise one that --skip-android (or
        # missing signing material) would leave out.
        include_apks = signing_ok and not args.skip_android
        include_installer = "desktop" in targets and not args.skip_installer
        assets = collect_content_assets(
            repo_root,
            release_dir,
            version,
            include_apks=include_apks,
            include_installer=include_installer,
        )
        print("  7. Assets   :")
        _print_asset_requirements(
            targets=targets,
            version=version,
            skip_installer=args.skip_installer,
            assets=assets,
        )
        for asset in assets:
            print(f"      {asset.name}  ({_human_size(asset.path.stat().st_size)})")
        print("      SHA256SUMS.txt (after the merge)")
        if (release_dir / "manifest.json").is_file():
            print("      manifest.json")
        # The updater manifest is rebuilt from these assets on a real run;
        # build it in memory here so the plan shows exactly which platforms
        # clients would be offered (no file is written, nothing is fetched).
        slug = args.repo or "OWNER/REPO"
        try:
            document = build_manifest_document(assets, version, tag, slug)
        except ReleaseError as exc:
            print(f"      latest.json  NOT GENERATED: {exc}")
        else:
            keys = ", ".join(sorted(document["platforms"]))
            print(
                f"      latest.json  ({len(document['platforms'])} platform(s): {keys})"
            )
            if args.repo is None:
                print("                   (OWNER/REPO from origin at release time)")
            if args.sign_key is not None:
                print(
                    f"                   would be signed with key file: {args.sign_key}"
                )
            else:
                print("                   unsigned (pass --sign-key to sign)")
    else:
        print("  7. Assets   : (produced by step 1; nothing built yet)")
        _print_asset_requirements(
            targets=targets,
            version=version,
            skip_installer=args.skip_installer,
            assets=None,
        )
        print("      latest.json is rebuilt from the produced assets")
    print()
    print("Re-run without --dry-run to execute the plan.")
    return 0


def _release_id(release: dict[str, Any]) -> int:
    """The numeric id of a release payload, or a clear ReleaseError."""
    try:
        return int(release["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError(
            f"release payload has no usable id: {release.get('id')!r}"
        ) from exc


def _run_release(args: argparse.Namespace, repo_root: Path) -> int:
    """Execute a (real or dry-run) release for the parsed arguments."""
    package_version = read_version(repo_root)
    version = args.version.removeprefix("v") if args.version else package_version
    if version.strip().lower() == "unreleased":
        raise ReleaseError(
            "'Unreleased' is not a releasable version; bump __version__ and "
            "turn the CHANGELOG section into '## v<version>' first"
        )
    tag = f"v{version}"
    # The tag is whatever goes on the release page; reject an input that git
    # would refuse before the bundles and the APK are built.
    check_release_tag(version, tag)
    targets = _parse_targets(args.targets)
    # Notes first: a missing or empty CHANGELOG section is the most actionable
    # error, and it also covers the "released a version that was never
    # documented" case before the version-consistency gate fires.  This also
    # proves the top section is for *this* version, not a newer or older one.
    notes = check_changelog_entry(repo_root / "CHANGELOG.md", version)

    # A mismatch here is how a release ends up shipping last build's assets
    # under this build's tag: the bundles are built from ``__version__`` while
    # the release directory is looked up by ``--version``.
    if version != package_version:
        raise ReleaseError(
            f"--version {version} does not match __version__ "
            f"{package_version}; bump src/novel_downloader/__init__.py"
        )
    check_android_version(repo_root, version)

    if args.dry_run:
        return _dry_run(
            args,
            repo_root=repo_root,
            version=version,
            tag=tag,
            targets=targets,
            notes=notes,
        )

    # Pre-build credential gate: the tracked files are exactly what the source
    # bundles are built from, so a token here would ship in every artifact.
    # It runs before the token lookup and the worktree guard, and long before
    # any PyInstaller/gradle work, so it fails in seconds.
    if args.skip_secret_scan:
        print(
            "warning: tracked-file secret scan skipped (--skip-secret-scan); "
            "the build is not credential-checked"
        )
    else:
        scan_release_secrets(repo_root)

    slug = args.repo or resolve_slug(repo_root)
    print(f"==> Releasing {PROJECT_NAME} {version} as {tag} to {slug}")

    token = github_token(repo_root)
    if not token:
        raise ReleaseError(
            "no GitHub token available: set GITHUB_TOKEN (or GH_TOKEN), or "
            "store a PAT where `git credential fill` can find it "
            "(protocol=https, host=github.com)"
        )

    # Credentials first (cheap, and the most common failure), then the
    # working-tree guard: only a real release tags HEAD, so only a real
    # release must be clean.  A dry run stays read-only.
    if not args.allow_dirty:
        require_clean_worktree(repo_root)

    # 1. PyInstaller bundles ------------------------------------------------
    if args.skip_build:
        print("==> Build skipped (--skip-build)")
    else:
        # The installer is a required asset for the desktop target, so a host
        # without ISCC must be rejected before the (tens of minutes) bundle
        # build rather than after it, when the missing exe would only surface
        # as a silent portable-only release.
        if "desktop" in targets and not args.skip_installer:
            iscc = find_inno_setup()
            if iscc is None:
                raise ReleaseError(
                    "the desktop target requires the Inno Setup installer, but "
                    "ISCC was not found; install Inno Setup 6 "
                    "(https://jrsoftware.org/isinfo.php) or pass "
                    "--skip-installer to publish the portable zip only"
                )
            print(f"==> Inno Setup compiler: {iscc}")
        build_cmd = _build_command(targets)
        _announce(build_cmd)
        proc = _run(build_cmd, cwd=repo_root, timeout=BUILD_TIMEOUT, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise ReleaseError(
                f"build_release.py exited with {proc.returncode}: "
                + _redact(" | ".join(detail[-8:]), (token,))
            )
        # The Inno Setup installer is produced by build_desktop.py, not by
        # build_release.py.  Without this step a "one command" release quietly
        # ships the portable zip only, and the installer has to be built and
        # uploaded by hand afterwards.
        if "desktop" in targets and not args.skip_installer and sys.platform == "win32":
            installer_cmd = [
                sys.executable,
                str(SCRIPTS_DIR / "build_desktop.py"),
            ]
            _announce(installer_cmd)
            proc = _run(
                installer_cmd, cwd=repo_root, timeout=BUILD_TIMEOUT, check=False
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise ReleaseError(
                    f"build_desktop.py exited with {proc.returncode}: "
                    + _redact(" | ".join(detail[-8:]), (token,))
                )

    release_dir = find_release_dir(repo_root, version)
    if release_dir is None:
        raise ReleaseError(
            f"no dist/release/{PROJECT_NAME}-{version}-* directory; run "
            "without --skip-build first"
        )
    # A directory left by a failed or partial build is not a release.  This
    # runs before the Android build and before any tag/API call, so an
    # incomplete --skip-build directory cannot publish anything.
    verify_release_dir(release_dir, version, targets)

    # 2. Android release APK ------------------------------------------------
    android_built = False
    if args.skip_android:
        print("==> Android skipped (--skip-android)")
    else:
        signing_ok, signing_reason = android_signing_available(repo_root)
        if not signing_ok:
            # Deliberately a warning, not an error: CI has no signing
            # material, and a release of the desktop/CLI bundles must not be
            # blocked by it.
            print(f"warning: Android release APK skipped: {signing_reason}")
        else:
            print(f"==> Android: {signing_reason}")
            gradle_cmd = gradle_command(repo_root)
            _announce(gradle_cmd)
            proc = _run(
                gradle_cmd,
                cwd=repo_root / "android",
                timeout=GRADLE_TIMEOUT,
                check=False,
            )
            apks = find_release_apks(repo_root)
            if proc.returncode != 0:
                # Windows gradlew.bat can surface a stale ERRORLEVEL (1)
                # even on BUILD SUCCESSFUL, so a produced APK plus the
                # success marker beats the exit code.
                succeeded = "BUILD SUCCESSFUL" in (proc.stdout or "") + (
                    proc.stderr or ""
                )
                if apks and succeeded:
                    print(
                        "warning: gradlew reported exit "
                        f"{proc.returncode} but printed BUILD SUCCESSFUL and "
                        "produced the APK; continuing"
                    )
                else:
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    raise ReleaseError(
                        f"gradlew assembleRelease exited with {proc.returncode}: "
                        + _redact(" | ".join(detail[-8:]), (token,))
                    )
            if not apks:
                raise ReleaseError(
                    "assembleRelease finished but no APK found under "
                    f"android/{_APK_GLOB}"
                )
            # Android 15+ refuses to load 4 KB-aligned native libraries on
            # 16 KB page-size devices.  A single dependency built without
            # -Wl,-z,max-page-size=16384 silently produces an APK that
            # installs but crashes, so this is a hard gate, not a warning.
            for apk in apks:
                check_apk_alignment(apk)
            android_built = True

    # 3. Assets + merged checksums -------------------------------------------
    content = collect_content_assets(
        repo_root,
        release_dir,
        version,
        include_apks=android_built,
        include_installer=("desktop" in targets and not args.skip_installer),
    )
    # --skip-build resumes a directory that may predate the installer, and the
    # installer build can also have been skipped by hand; both are release
    # blockers for the desktop target (exit 2, before tag/API calls).
    verify_release_requirements(
        content,
        targets=targets,
        version=version,
        skip_installer=args.skip_installer,
    )
    # The updater manifest is rebuilt from the assets that actually exist, so
    # a missing platform shows up as a missing entry instead of a stale URL.
    content.append(
        build_latest_manifest(
            content, release_dir, version, tag, slug, sign_key=args.sign_key
        )
    )
    audit_release_content(content)
    sums_path = release_dir / "SHA256SUMS.txt"
    lines = merge_sums(sums_path, [(asset.name, asset.sha256) for asset in content])
    print(f"==> SHA256SUMS: {len(lines)} entries in {sums_path}")
    # The two files are published together and a client may verify against
    # either, so a disagreement between them is a release-blocking bug.
    validate_sums_against_manifest(sums_path, release_dir / "latest.json")

    upload_assets = [asset for asset in content if asset.name != "latest.json"]
    upload_assets.append(AssetSpec(sums_path, sums_path.name, _sha256_file(sums_path)))
    manifest_path = release_dir / "manifest.json"
    if manifest_path.is_file():
        upload_assets.append(
            AssetSpec(manifest_path, manifest_path.name, _sha256_file(manifest_path))
        )
    # The update manifest goes last: it is the document clients trust before
    # downloading anything, so every file it points at must already be there.
    upload_assets.extend(asset for asset in content if asset.name == "latest.json")
    _ensure_unique_asset_names(upload_assets)

    # 4./5. Tag and push -----------------------------------------------------
    local_sha = local_tag_commit(repo_root, tag)
    head_sha = head_commit(repo_root)
    remote_sha = remote_tag_commit(repo_root, tag)
    verify_tag_target(
        tag, local_sha=local_sha, head_sha=head_sha, remote_sha=remote_sha
    )
    if local_sha is not None:
        print(f"==> Tag {tag} exists locally at {local_sha[:12]}, skipping creation")
    else:
        create_tag(repo_root, tag)
        local_sha = head_sha or local_tag_commit(repo_root, tag)
        if local_sha is None:
            raise ReleaseError(
                f"created tag {tag} but cannot resolve it to a commit; "
                "refusing to publish"
            )
    if remote_sha is not None:
        print(f"==> Tag {tag} is already on the remote at {remote_sha[:12]}")
    else:
        # A concurrent run pushing the same tag is handled by git (same commit:
        # up to date; different commit: rejected) and by the REST fallback,
        # which refuses to move an existing ref.
        push_tag(repo_root, tag, token, slug, api_base=args.api_base)

    # 6. Release + assets ----------------------------------------------------
    client = GitHubClient(slug, token, api_base=args.api_base)
    release = client.get_release_by_tag(tag)
    publish_after_verify = False
    if release is None:
        # A previous attempt may have died after creating the draft.  The tag
        # lookup cannot see drafts, so ask the list endpoint before creating
        # a second one.
        release = client.find_draft_release(tag)
    if release is None:
        # Create as a draft: until every asset is uploaded and verified, the
        # release must not be visible.  A later network failure then leaves a
        # resumable draft, never a half-populated "published" release.
        print(
            f"==> Creating GitHub release {tag} (draft; published after verification)"
        )
        release = client.create_release(
            tag,
            tag,
            notes,
            draft=True,
            prerelease=args.prerelease,
        )
        publish_after_verify = not args.draft
    else:
        release_id = _release_id(release)
        state = "draft" if release.get("draft") else "release"
        print(f"==> {state.capitalize()} for {tag} already exists (id {release_id})")
        if (release.get("body") or "") != notes and notes:
            print("    notes differ from CHANGELOG.md, updating")
            release = client.update_release(release_id, notes)
        # A previous run may have died after creating the draft.
        if release.get("draft") and not args.draft:
            publish_after_verify = True

    print(f"==> Uploading {len(upload_assets)} asset(s)")
    sync_assets(client, release, upload_assets)
    verify_assets(client, tag, upload_assets, release_id=_release_id(release))

    if publish_after_verify:
        print(f"==> Publishing release {tag}")
        release = client.publish_release(_release_id(release))
    elif args.draft:
        print(f"==> Release {tag} left as a draft (--draft)")

    url = release.get("html_url")
    print()
    print(
        f"Release complete: {url or f'https://{GITHUB_HOST}/{slug}/releases/tag/{tag}'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and run the release; 0 on success, 2 on ReleaseError."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        metavar="X.Y.Z",
        help="version to release (default: __version__ from the package)",
    )
    parser.add_argument(
        "--targets",
        default="all",
        metavar="LIST",
        help=f"comma-separated bundle targets (default: all of "
        f"{','.join(DEFAULT_TARGETS)})",
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="GitHub repository to publish to (default: from the origin remote)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the whole plan and execute nothing",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse the existing dist/release bundle directory",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="tag even when the working tree has uncommitted changes",
    )
    parser.add_argument(
        "--sign-key",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "sign latest.json with the 32-byte Ed25519 seed in PATH "
            "(clients only verify once the public key is trusted)"
        ),
    )
    parser.add_argument(
        "--skip-android",
        action="store_true",
        help="do not build (or upload) the Android release APK",
    )
    parser.add_argument(
        "--skip-installer",
        action="store_true",
        help=(
            "publish the portable desktop zip instead of the Inno Setup "
            "installer; latest.json then advertises no windows-x64-installer "
            "platform (the portable zip must be present)"
        ),
    )
    parser.add_argument(
        "--skip-secret-scan",
        action="store_true",
        help=(
            "DANGEROUS: skip the tracked-file credential scan "
            "(scripts/scan_secrets.py) before the build; only for a verified "
            "false positive, not recommended"
        ),
    )
    parser.add_argument(
        "--draft", action="store_true", help="create the release as a draft"
    )
    parser.add_argument(
        "--prerelease",
        action="store_true",
        help="mark the release as a prerelease",
    )
    parser.add_argument(
        "--api-base",
        default=GITHUB_API,
        metavar="URL",
        help=f"GitHub REST API base (default: {GITHUB_API})",
    )
    args = parser.parse_args(argv)

    try:
        return _run_release(args, REPO_ROOT)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
