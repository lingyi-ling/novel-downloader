#!/usr/bin/env python3
"""One-command release for novel-downloader.

Usage::

    .venv/Scripts/python.exe scripts/release.py --dry-run
    .venv/Scripts/python.exe scripts/release.py
    .venv/Scripts/python.exe scripts/release.py --version 1.3.0
    .venv/Scripts/python.exe scripts/release.py --skip-build --skip-android
    GITHUB_TOKEN=... python scripts/release.py --repo owner/name

What a full run does, in order:

1. Read the version from ``src/novel_downloader/__init__.py`` (or ``--version``)
   and extract that version's section from ``CHANGELOG.md`` as the release
   notes.  The run fails early if the section is missing.
2. Build the three PyInstaller bundles by invoking
   ``scripts/build_release.py --targets cli,api,desktop`` (same validation,
   zipping and SHA256SUMS handling as a manual build).
3. Build the Android release APK with ``gradlew assembleRelease``.  When the
   signing material (``android/keystore.properties`` + keystore file) is
   missing, this step warns and is skipped instead of failing the release.
4. Merge the checksums of the APK (and the Inno Setup installer, when present)
   into the build's ``SHA256SUMS.txt``.
5. Create the ``v<version>`` tag locally and push it.  Both steps are skipped
   when the tag already exists locally / on the remote.  The push uses git's
   own credential chain, or an explicit token URL when ``GITHUB_TOKEN`` /
   ``GH_TOKEN`` is set; the REST token comes from ``GITHUB_TOKEN`` /
   ``GH_TOKEN`` or from ``git credential fill`` (protocol=https,
   host=github.com).  No credential is ever hardcoded or printed.
6. Create the GitHub Release via the REST API (or reuse an existing one for
   the tag, updating its notes when they differ), then upload every asset.
   An asset whose remote digest matches the local SHA-256 is skipped; one
   that differs is deleted and re-uploaded.  Re-running a finished release
   therefore does nothing but a GET.

``--dry-run`` prints the whole plan (notes, commands, assets) and executes
nothing: no subprocess, no network.  ``--skip-build`` reuses an existing
``dist/release/novel-downloader-<version>-*/`` directory, which is how a run
that died midway (e.g. network drop) is resumed without rebuilding.

Exit codes: 0 success; 2 for a ``ReleaseError`` (bad input, failed build,
API error) and for argparse usage errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
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
GITHUB_API = "https://api.github.com"
GITHUB_HOST = "github.com"
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
CHUNK_SIZE = 1024 * 1024
HTTP_TIMEOUT = 120.0
CREDENTIAL_TIMEOUT = 30.0
GRADLE_TIMEOUT = 2700.0
# PyInstaller builds of three bundles take tens of minutes: no timeout.
BUILD_TIMEOUT: float | None = None

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_SUMS_LINE_RE = re.compile(r"^(?P<sha>[0-9a-fA-F]{64})\s+(?P<name>\S.*)$")
_REMOTE_SLUG_RE = re.compile(
    r"github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)"
    r"(?:\.git)?/?\s*$"
)
_TOKEN_IN_URL_RE = re.compile(r"x-access-token:[^@\s]+@")
_APK_GLOB = "app/build/outputs/apk/release/*.apk"


class ReleaseError(RuntimeError):
    """A release step failed; the message is safe to print."""


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
            f"cannot derive OWNER/NAME from origin URL {lines[0]!r}; "
            "pass --repo OWNER/NAME explicitly"
        )
    return slug


# ---------------------------------------------------------------------------
# Subprocess plumbing (tests monkeypatch the module-level ``subprocess``)
# ---------------------------------------------------------------------------


def _display_command(command: list[str] | tuple[str, ...]) -> str:
    """A printable form of *command* with any embedded token redacted."""
    return _TOKEN_IN_URL_RE.sub("x-access-token:***@", " ".join(command))


def _run(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *command* capturing UTF-8 output; raise ReleaseError on failure."""
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
            f"command timed out after {timeout}s: {_display_command(command)}"
        ) from exc
    if check and proc.returncode != 0:
        # stderr/stdout may be None on CompletedProcess, so guard the strip.
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        raise ReleaseError(
            f"command failed with exit code {proc.returncode} "
            f"({_display_command(command)}): " + " | ".join(detail[-8:])
        )
    return proc


def _announce(command: list[str] | tuple[str, ...]) -> None:
    print(f"==> {_display_command(command)}")


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


def android_asset_names(version: str, apks: list[Path]) -> list[str]:
    """Release asset names for the APKs, in the same order as *apks*."""
    if len(apks) == 1:
        return [f"{PROJECT_NAME}-{version}-android.apk"]
    return [f"{PROJECT_NAME}-{version}-android-{apk.stem}.apk" for apk in apks]


def collect_content_assets(
    repo_root: Path, release_dir: Path, version: str, *, include_apks: bool
) -> list[AssetSpec]:
    """
    The release's "content" assets: bundle zips, (optional) APK, installer.

    ``SHA256SUMS.txt`` and ``manifest.json`` are metadata produced alongside
    the content and are added later, after the checksums are merged.  APKs are
    only included when the Android build ran in this release (or, for the
    dry-run preview, when a signed output is already on disk).
    """
    metadata = {"SHA256SUMS.txt", "manifest.json"}
    files = sorted(
        path
        for path in release_dir.iterdir()
        if path.is_file() and path.name not in metadata
    )
    assets = [AssetSpec(path, path.name, _sha256_file(path)) for path in files]

    if include_apks:
        apks = find_release_apks(repo_root)
        names = android_asset_names(version, apks)
        assets.extend(
            AssetSpec(apk, name, _sha256_file(apk))
            for apk, name in zip(apks, names, strict=True)
        )

    installer = repo_root / "dist" / f"NovelDownloader-setup-{version}-x64.exe"
    if installer.is_file():
        assets.append(AssetSpec(installer, installer.name, _sha256_file(installer)))
    return assets


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
        if token:
            return token
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
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise ReleaseError(
                f"network error calling {method} {url}: {exc.reason}"
            ) from exc
        payload = _parse_json(raw)
        if status not in expected:
            message = (
                payload.get("message") if isinstance(payload, dict) else None
            ) or raw[:200].decode("utf-8", "replace")
            raise ReleaseError(
                f"GitHub API {method} {url} returned HTTP {status}: {message}"
            )
        return Reply(status, payload)

    def _json(self, reply: Reply, context: str) -> dict[str, Any]:
        if not isinstance(reply.payload, dict):
            raise ReleaseError(f"{context}: unexpected API body: {reply.payload!r}")
        return reply.payload

    # -- endpoints ---------------------------------------------------------

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        """The release published at *tag*, or ``None`` when there is none."""
        url = f"{self._api}/repos/{self._slug}/releases/tags/{quote(tag)}"
        reply = self._request("GET", url, expected=(200, 404))
        if reply.status == 404:
            return None
        return self._json(reply, f"release lookup for {tag}")

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

    def delete_asset(self, asset_id: int) -> None:
        """Delete one release asset by id."""
        url = f"{self._api}/repos/{self._slug}/releases/assets/{asset_id}"
        self._request("DELETE", url, expected=(200, 204))

    def upload_asset(self, upload_url: str, asset: AssetSpec) -> None:
        """Upload *asset* to the release's upload endpoint."""
        base = upload_url.split("{", 1)[0].rstrip("/")
        if not base:
            raise ReleaseError(
                "release payload has no upload_url; cannot upload assets"
            )
        url = f"{base}?name={quote(asset.name)}"
        self._request(
            "POST",
            url,
            data=asset.path.read_bytes(),
            content_type="application/octet-stream",
            expected=(200, 201),
        )
        print(
            f"  asset {asset.name}: uploaded ({_human_size(asset.path.stat().st_size)})"
        )


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
    Make the release contain exactly *assets* (others are left alone).

    Idempotent: an asset whose remote digest equals the local SHA-256 is
    skipped; an asset with the same name but different content is deleted and
    re-uploaded; a missing asset is simply uploaded.
    """
    remote: dict[str, dict[str, Any]] = {}
    for item in release.get("assets") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            remote[str(item["name"])] = item

    for asset in assets:
        existing = remote.get(asset.name)
        if existing is not None and _remote_digest(existing) == asset.sha256:
            print(f"  asset {asset.name}: up to date, skipping")
            continue
        if existing is not None:
            print(f"  asset {asset.name}: digest differs, replacing")
            client.delete_asset(int(existing["id"]))
        client.upload_asset(str(release.get("upload_url", "")), asset)


def verify_assets(client: GitHubClient, tag: str, assets: list[AssetSpec]) -> None:
    """
    Re-read the release and assert every intended asset is actually present.

    Uploading is the one step whose failure used to be invisible: a release
    published by a concurrent run (or a partially failed upload) left the
    release looking complete while a bundle or the APK was missing.  Checking
    the final state costs one request and turns that into a hard error.
    """
    current = client.get_release_by_tag(tag)
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


def tag_exists_local(repo_root: Path, tag: str) -> bool:
    """Whether *tag* already exists in the local repository."""
    proc = _run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        cwd=repo_root,
        check=False,
    )
    return proc.returncode == 0


def remote_tag_exists(repo_root: Path, tag: str) -> bool:
    """Whether *tag* is already on the origin remote (one network query)."""
    proc = _run(
        ["git", "ls-remote", "origin", f"refs/tags/{tag}"],
        cwd=repo_root,
        check=False,
    )
    return bool(proc.stdout.strip())


def create_tag(repo_root: Path, tag: str) -> None:
    """
    Create *tag* (lightweight, like the repository's existing v1.1.0/v1.2.0,
    and independent of any git user identity being configured).
    """
    _announce(["git", "tag", tag])
    _run(["git", "tag", tag], cwd=repo_root)


def push_tag(repo_root: Path, tag: str, token: str | None, slug: str) -> None:
    """
    Push *tag* to origin.

    With an environment token (CI) the push goes to an explicit authenticated
    URL — the token is redacted wherever the command is printed.  Without
    one, plain ``git push`` lets git resolve credentials through its own
    credential helper chain (the same store ``git credential fill`` reads).
    """
    if token:
        url = f"https://x-access-token:{token}@{GITHUB_HOST}/{slug}.git"
        command: list[str] | tuple[str, ...] = ["git", "push", url, tag]
    else:
        command = ["git", "push", "origin", tag]
    _announce(command)
    _run(command, cwd=repo_root)


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
    _print_notes_preview(notes, version)

    print("Plan:")
    if args.skip_build:
        print("  1. Build    : skipped (--skip-build)")
        print(f"              release dir: {release_dir or 'NOT FOUND (build first)'}")
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
    print(f"  5. Push     : git push origin {tag} (skipped if already on the remote)")
    flags = ""
    if args.draft:
        flags += ", draft"
    if args.prerelease:
        flags += ", prerelease"
    print(f"  6. Release  : create {tag} on GitHub with the notes above{flags}")
    if release_dir is not None:
        assets = collect_content_assets(
            repo_root, release_dir, version, include_apks=True
        )
        print("  7. Assets   :")
        for asset in assets:
            print(f"      {asset.name}  ({_human_size(asset.path.stat().st_size)})")
        print("      SHA256SUMS.txt (after the merge)")
        if (release_dir / "manifest.json").is_file():
            print("      manifest.json")
    else:
        print("  7. Assets   : (produced by step 1; nothing built yet)")
    print()
    print("Re-run without --dry-run to execute the plan.")
    return 0


def _run_release(args: argparse.Namespace, repo_root: Path) -> int:
    """Execute a (real or dry-run) release for the parsed arguments."""
    version = (
        args.version.removeprefix("v") if args.version else read_version(repo_root)
    )
    if version.strip().lower() == "unreleased":
        raise ReleaseError(
            "'Unreleased' is not a releasable version; bump __version__ and "
            "turn the CHANGELOG section into '## v<version>' first"
        )
    tag = f"v{version}"
    targets = _parse_targets(args.targets)
    notes = extract_changelog_notes(repo_root / "CHANGELOG.md", version)

    if args.dry_run:
        return _dry_run(
            args,
            repo_root=repo_root,
            version=version,
            tag=tag,
            targets=targets,
            notes=notes,
        )

    slug = args.repo or resolve_slug(repo_root)
    print(f"==> Releasing {PROJECT_NAME} {version} as {tag} to {slug}")
    if not notes:
        print("warning: the CHANGELOG section for this version is empty")

    token = github_token(repo_root)
    if not token:
        raise ReleaseError(
            "no GitHub token available: set GITHUB_TOKEN (or GH_TOKEN), or "
            "store a PAT where `git credential fill` can find it "
            "(protocol=https, host=github.com)"
        )

    # 1. PyInstaller bundles ------------------------------------------------
    if args.skip_build:
        print("==> Build skipped (--skip-build)")
    else:
        build_cmd = _build_command(targets)
        _announce(build_cmd)
        proc = _run(build_cmd, cwd=repo_root, timeout=BUILD_TIMEOUT, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise ReleaseError(
                f"build_release.py exited with {proc.returncode}: "
                + " | ".join(detail[-8:])
            )
        # The Inno Setup installer is produced by build_desktop.py, not by
        # build_release.py.  Without this step a "one command" release quietly
        # ships the portable zip only, and the installer has to be built and
        # uploaded by hand afterwards.
        if "desktop" in targets and sys.platform == "win32":
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
                    + " | ".join(detail[-8:])
                )

    release_dir = find_release_dir(repo_root, version)
    if release_dir is None:
        raise ReleaseError(
            f"no dist/release/{PROJECT_NAME}-{version}-* directory; run "
            "without --skip-build first"
        )

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
                        + " | ".join(detail[-8:])
                    )
            if not apks:
                raise ReleaseError(
                    "assembleRelease finished but no APK found under "
                    f"android/{_APK_GLOB}"
                )
            android_built = True

    # 3. Assets + merged checksums -------------------------------------------
    content = collect_content_assets(
        repo_root, release_dir, version, include_apks=android_built
    )
    sums_path = release_dir / "SHA256SUMS.txt"
    lines = merge_sums(sums_path, [(asset.name, asset.sha256) for asset in content])
    print(f"==> SHA256SUMS: {len(lines)} entries in {sums_path}")

    upload_assets = list(content)
    upload_assets.append(AssetSpec(sums_path, sums_path.name, _sha256_file(sums_path)))
    manifest_path = release_dir / "manifest.json"
    if manifest_path.is_file():
        upload_assets.append(
            AssetSpec(manifest_path, manifest_path.name, _sha256_file(manifest_path))
        )

    # 4./5. Tag and push -----------------------------------------------------
    if tag_exists_local(repo_root, tag):
        print(f"==> Tag {tag} exists locally, skipping creation")
    else:
        create_tag(repo_root, tag)
    if remote_tag_exists(repo_root, tag):
        print(f"==> Tag {tag} is already on the remote, skipping push")
    else:
        push_tag(repo_root, tag, token, slug)

    # 6. Release + assets ----------------------------------------------------
    client = GitHubClient(slug, token, api_base=args.api_base)
    release = client.get_release_by_tag(tag)
    if release is None:
        print(f"==> Creating GitHub release {tag}")
        release = client.create_release(
            tag,
            tag,
            notes,
            draft=args.draft,
            prerelease=args.prerelease,
        )
    else:
        release_id = release.get("id")
        print(f"==> Release for {tag} already exists (id {release_id}), reusing")
        if (release.get("body") or "") != notes and notes:
            print("    notes differ from CHANGELOG.md, updating")
            release = client.update_release(int(str(release_id)), notes)

    print(f"==> Uploading {len(upload_assets)} asset(s)")
    sync_assets(client, release, upload_assets)
    verify_assets(client, tag, upload_assets)

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
        "--skip-android",
        action="store_true",
        help="do not build (or upload) the Android release APK",
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
