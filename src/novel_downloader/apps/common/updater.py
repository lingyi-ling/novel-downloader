#!/usr/bin/env python3
"""
novel_downloader.apps.common.updater
------------------------------------

UI-independent helpers behind the desktop client's update check.

The module deliberately contains no GUI imports and performs network access
only inside functions the caller opts into.  It answers three questions:

* which release version is newer than the running one (:func:`parse_version`,
  :func:`is_newer`);
* which release asset belongs to this machine (:func:`detect_platform`,
  :func:`select_asset`, :func:`parse_manifest`);
* can a downloaded file be trusted (:func:`verify_sha256`).

The asset names it recognises mirror the ones produced by
``scripts/build_release.py`` and ``scripts/release.py``::

    novel-cli-<version>-<os>-<arch>.zip
    novel-api-<version>-<os>-<arch>.zip
    NovelDownloader-<version>-<os>-<arch>.zip      (desktop bundle)
    novel-downloader-<version>-android.apk
    NovelDownloader-setup-<version>-x64.exe        (Inno Setup installer)

:func:`download_asset` fetches an asset, verifies its checksum and stops
there: nothing here executes, unpacks or replaces a running program.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = [
    "DEFAULT_CHECK_INTERVAL",
    "DEFAULT_REPO",
    "DEFAULT_TIMEOUT",
    "ReleaseAsset",
    "UpdateCheck",
    "UpdateInfo",
    "MANIFEST_SCHEMA",
    "TRUSTED_UPDATE_KEYS",
    "check_due",
    "check_for_update",
    "fetch_latest_release",
    "fetch_update_manifest",
    "load_update_state",
    "manifest_url",
    "parse_update_manifest",
    "release_api_url",
    "save_update_state",
    "select_manifest_asset",
    "verify_manifest_signature",
    "detect_platform",
    "download_asset",
    "is_newer",
    "parse_manifest",
    "parse_version",
    "resolve_mirror",
    "select_asset",
    "verify_sha256",
]

#: Timeout applied to update-related requests, in seconds.
DEFAULT_TIMEOUT = 8.0

_USER_AGENT = "novel-downloader-updater"
_CHUNK_SIZE = 1024 * 1024
#: Hard ceiling for one asset transfer (the largest release asset is ~70 MiB).
#: A hostile or broken manifest cannot make the client fill the disk.
_MAX_ASSET_BYTES = 1024**3
_MAX_FILENAME = 120

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Windows resolves these names before the filesystem, with or without an
#: extension: ``con`` blocks the writer and ``NUL`` silently swallows the
#: bytes, so a sanitised name must never be one of them.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(10)}
    | {f"lpt{index}" for index in range(10)}
)

#: Project spellings used by the release scripts (``release.py`` lowercases to
#: ``novel-downloader-``, the desktop bundle ships as ``NovelDownloader-``).
_PROJECT_PREFIXES = ("noveldownloader-", "novel-downloader-")

#: Normalised machine names; anything else is passed through lowercased.
_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
}

#: Normalised operating-system names; anything else is passed through.
_OS_ALIASES = {
    "windows": "windows",
    "win32": "windows",
    "darwin": "macos",
    "macos": "macos",
    "linux": "linux",
}

#: ``AppId`` of the Inno Setup installer (see ``packaging/noveldl_installer.iss``).
#: Inno writes its uninstall entry as ``<AppId>_is1`` under both the per-user
#: and machine-wide uninstall keys, so an install into a directory other than
#: the default one is still recognisable from the registry.
_INSTALLER_APP_ID = "{7A1C0B4D-52E8-4F6A-9B3D-10C2E5F7A821}"
_INSTALLER_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    rf"\{_INSTALLER_APP_ID}_is1"
)
#: Registry values that point at the installed program: Inno records ``{app}``
#: as ``InstallLocation`` and the launcher as ``DisplayIcon`` (often suffixed
#: with a ``",0"`` icon index).
_INSTALLER_LOCATION_VALUES = ("InstallLocation", "DisplayIcon")
#: Inno generates this uninstaller next to the installed executable.  It is a
#: fallback for the rare install whose registry entry is missing or unreadable.
_INSTALLER_MARKERS = ("unins000.exe",)


@dataclass(frozen=True)
class ReleaseAsset:
    """One downloadable file attached to a release."""

    name: str
    url: str
    size: int | None
    sha256: str | None


@dataclass(frozen=True)
class UpdateInfo:
    """The result of interpreting one release payload."""

    current: str
    latest: str
    available: bool
    notes_url: str | None
    asset: ReleaseAsset | None
    assets: tuple[ReleaseAsset, ...]


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    """
    Parse ``"1.2.3"`` (optionally ``v``-prefixed or ``-suffixed``) for compares.

    Only the leading numeric run of each dot-separated chunk is kept, so
    ``"v1.10.0-rc1"`` becomes ``(1, 10, 0)``.  Pre-release and build-metadata
    suffixes are dropped here because :func:`is_newer` orders them separately.
    Unparseable input yields ``(0,)`` instead of raising, because release tags
    are remote data.

    :param text: a version string, e.g. ``"v1.2.3"``.
    :return: a tuple of integers, never empty.
    """
    core = str(text or "").strip().split("+", 1)[0].split("-", 1)[0].lstrip("vV")
    parts: list[int] = []
    for chunk in core.split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def _version_key(
    text: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, object], ...]]:
    """
    A total-order key for a release version, including its pre-release part.

    ``(numeric_core, release_rank, identifiers)`` mirrors SemVer precedence
    closely enough for release tags: a final release outranks every pre-release
    of the same core, pre-release identifiers compare field by field (numeric
    ones numerically, alphanumerics case-insensitively) and build metadata
    (``+...``) never affects ordering.

    :param text: a version or tag, e.g. ``"v1.5.0-rc2+build.7"``.
    :return: a tuple that compares correctly with another version key.
    """
    raw = str(text or "").strip().lstrip("vV")
    without_build = raw.split("+", 1)[0]
    _core, separator, prerelease = without_build.partition("-")
    identifiers: list[tuple[int, object]] = []
    for part in prerelease.split(".") if separator else ():
        if part.isdigit():
            # Numbers sort below alphanumerics (``(0, n)`` vs ``(1, s)``), so
            # the second element is never compared across the two kinds.
            identifiers.append((0, int(part)))
        elif part:
            identifiers.append((1, part.lower()))
    return (
        parse_version(without_build),
        0 if separator else 1,
        tuple(identifiers),
    )


def is_newer(latest: str, current: str) -> bool:
    """
    Whether ``latest`` is a strictly newer version than ``current``.

    Missing trailing components count as zero, so ``1.2.3.0`` and ``1.2.3``
    compare equal rather than one being "newer".  A final release outranks its
    own pre-releases (``1.5.0`` > ``1.5.0-rc1``), while an older or equal
    version -- including a downgrade attempt -- is never "newer".

    :param latest: the candidate version.
    :param current: the version in use.
    :return: ``True`` only when ``latest`` > ``current``.
    """
    left_core, left_rank, left_pre = _version_key(latest)
    right_core, right_rank, right_pre = _version_key(current)
    width = max(len(left_core), len(right_core))
    left = (left_core + (0,) * (width - len(left_core)), left_rank, left_pre)
    right = (right_core + (0,) * (width - len(right_core)), right_rank, right_pre)
    return left > right


# ---------------------------------------------------------------------------
# platforms and assets
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """
    Return this machine's release tag, e.g. ``"windows-x64"``.

    The spelling matches ``scripts/build_release.py::_platform_tag`` so asset
    names produced by the build can be matched directly.

    :return: ``"<os>-<arch>"``.
    """
    system = platform.system().strip().lower()
    os_name = _OS_ALIASES.get(system, system or "unknown")
    machine = platform.machine().strip().lower()
    arch = _ARCH_ALIASES.get(machine, machine or "unknown")
    return f"{os_name}-{arch}"


def _install_dir_from_registry_value(name: str, raw: object) -> Path | None:
    """
    Interpret one uninstall-registry value as the installation directory.

    ``InstallLocation`` is already a directory.  ``DisplayIcon`` points at the
    executable (or an icon) and may be quoted or followed by ``",0"``; its
    parent directory is what matters.  Malformed values yield ``None`` instead
    of raising, because the registry is just another untrusted input.

    :param name: ``"InstallLocation"`` or ``"DisplayIcon"``.
    :param raw: the decoded registry value.
    :return: the directory, or ``None``.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if name == "DisplayIcon":
        head, separator, tail = text.rpartition(",")
        if separator and tail.strip().isdigit():
            text = head
    text = text.strip().strip('"').strip()
    if not text:
        return None
    path = Path(text)
    if name == "DisplayIcon":
        path = path.parent
    return path


def _installer_registry_dirs() -> tuple[Path, ...]:
    """
    Installation directories recorded by the Inno Setup uninstaller.

    Both hives and both registry views are consulted because the installer is
    per-user (``HKCU``) but an elevated or 32/64-bit-mismatched install may
    have written elsewhere.  Missing keys, unsupported views and a missing
    ``winreg`` (non-Windows builds, test environments) are all normal: the
    caller falls back to the path-based check.

    :return: every recorded directory that could be identified, possibly empty.
    """
    if sys.platform != "win32":
        return ()
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always ships winreg
        return ()

    directories: list[Path] = []
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (
        0,
        getattr(winreg, "KEY_WOW64_32KEY", 0),
        getattr(winreg, "KEY_WOW64_64KEY", 0),
    )
    for hive in hives:
        for view in views:
            try:
                with winreg.OpenKey(
                    hive, _INSTALLER_UNINSTALL_KEY, 0, winreg.KEY_READ | view
                ) as key:
                    for value_name in _INSTALLER_LOCATION_VALUES:
                        try:
                            raw, _kind = winreg.QueryValueEx(key, value_name)
                        except OSError:
                            continue
                        directory = _install_dir_from_registry_value(value_name, raw)
                        if directory is not None and directory not in directories:
                            directories.append(directory)
            except OSError:
                # Key absent in this hive/view: the next one may still exist.
                continue
    return tuple(directories)


def _exe_belongs_to(exe: Path, directory: Path) -> bool:
    """Whether ``exe`` lives inside ``directory`` (case-insensitive on Windows)."""
    try:
        return exe.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return False


def _has_installer_marker(app_dir: Path) -> bool:
    """Whether Inno's uninstaller sits next to this executable."""
    for name in _INSTALLER_MARKERS:
        try:
            if (app_dir / name).is_file():
                return True
        except OSError:
            continue
    return False


def detect_install_shape() -> str:
    """
    How this copy was installed: ``"installer"``, ``"portable"`` or ``"source"``.

    A source checkout (not frozen) and every non-Windows build report
    ``"source"``.  On Windows the executable is matched against the Inno Setup
    uninstall entry first, so a user who chose a custom installation directory
    is still recognised as an ``"installer"`` copy.  When the registry entry is
    absent or unreadable, an ``unins000.exe`` marker next to the executable and
    finally the installer's default ``%LOCALAPPDATA%\\Programs\\NovelDownloader``
    location are tried.  Anything else frozen is a portable extraction.

    Asset selection uses this so a portable build replaces its own folder
    instead of silently installing a second copy.
    """
    if not getattr(sys, "frozen", False):
        return "source"
    if sys.platform != "win32":
        return "source"
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return "portable"

    for directory in _installer_registry_dirs():
        if _exe_belongs_to(exe, directory):
            return "installer"
    if _has_installer_marker(exe.parent):
        return "installer"

    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        try:
            installed = (Path(local) / "Programs" / "NovelDownloader").resolve()
            if _exe_belongs_to(exe, installed):
                return "installer"
        except (OSError, ValueError):
            pass
    return "portable"


def _find_asset(
    assets: Sequence[ReleaseAsset],
    *,
    prefix: str,
    suffix: str,
) -> ReleaseAsset | None:
    """First asset whose lowercased name has ``prefix`` and ``suffix``."""
    for asset in assets:
        name = asset.name.strip().lower()
        if name.startswith(prefix) and name.endswith(suffix):
            return asset
    return None


def _find_android_asset(assets: Sequence[ReleaseAsset]) -> ReleaseAsset | None:
    """
    The project's Android APK, preferring the canonical ``-android.apk`` name.

    ``scripts/release.py`` names a single APK ``novel-downloader-<ver>-android
    .apk`` and switches to ``novel-downloader-<ver>-android-<stem>.apk`` when a
    release publishes several APKs, so any project APK is an acceptable match;
    the canonical name simply wins when both are present.

    :param assets: every asset of the release, in payload order.
    :return: the chosen APK, or ``None``.
    """
    fallback: ReleaseAsset | None = None
    for asset in assets:
        name = asset.name.strip().lower()
        if not name.endswith(".apk") or not name.startswith(_PROJECT_PREFIXES):
            continue
        if name.endswith("-android.apk"):
            return asset
        if fallback is None:
            fallback = asset
    return fallback


def select_asset(
    assets: Sequence[ReleaseAsset],
    platform_tag: str,
    *,
    shape: str | None = None,
) -> ReleaseAsset | None:
    """
    Pick the release asset that belongs to ``platform_tag``.

    Preference order, using the project's naming rules:

    * Android: the single ``novel-downloader-<ver>-android.apk``, or the first
      project APK when the release publishes per-ABI names;
    * Windows x64 from the installer: the ``NovelDownloader-setup-<ver>-x64.exe``
      installer, falling back to the desktop bundle zip;
    * Windows x64 portable: the desktop bundle zip first (it replaces the
      portable folder), installer only as a fallback;
    * everything else: the ``NovelDownloader-<ver>-<platform>.zip`` bundle.

    CLI/API bundles are never returned: they are not what the desktop client
    would install.  When nothing matches, ``None`` is returned rather than a
    guess.

    :param assets: every asset of the release, in payload order.
    :param platform_tag: a tag as produced by :func:`detect_platform`.
    :param shape: :func:`detect_install_shape` result, when known.
    :return: the matching asset, or ``None``.
    """
    tag = str(platform_tag or "").strip().lower()
    if not tag:
        return None
    os_name = tag.split("-", 1)[0]
    if os_name == "android":
        return _find_android_asset(assets)
    if os_name == "windows" and tag.endswith("x64"):
        installer = _find_asset(
            assets, prefix="noveldownloader-setup-", suffix="-x64.exe"
        )
        bundle = _find_asset(assets, prefix="noveldownloader-", suffix=f"-{tag}.zip")
        if str(shape or "").strip().lower() == "portable" and bundle is not None:
            # A portable copy must replace its own folder, not install a
            # second copy into %LOCALAPPDATA%.
            return bundle
        if installer is not None:
            return installer
        if bundle is not None:
            return bundle
        return None
    return _find_asset(assets, prefix="noveldownloader-", suffix=f"-{tag}.zip")


def _normalise_sha(value: str) -> str | None:
    """Lowercase a ``sha256:<hex>`` (or bare) digest, or ``None`` if invalid."""
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    return text if _SHA256_RE.fullmatch(text) else None


def _asset_from_payload(item: Any) -> ReleaseAsset | None:
    """Read one release asset object; ``None`` when it is malformed."""
    if not isinstance(item, Mapping):
        return None
    raw_name = item.get("name")
    raw_url = item.get("browser_download_url") or item.get("url")
    if not isinstance(raw_name, str) or not isinstance(raw_url, str):
        return None
    name = raw_name.strip()
    url = raw_url.strip()
    if not name or not url:
        return None

    raw_size = item.get("size")
    size = (
        int(raw_size)
        if isinstance(raw_size, int)
        and not isinstance(raw_size, bool)
        and raw_size >= 0
        else None
    )

    sha256: str | None = None
    raw_sha = item.get("sha256")
    if isinstance(raw_sha, str):
        sha256 = _normalise_sha(raw_sha)
    if sha256 is None:
        digest = item.get("digest")
        if isinstance(digest, str):
            sha256 = _normalise_sha(digest)

    return ReleaseAsset(name=name, url=url, size=size, sha256=sha256)


def parse_manifest(
    payload: dict[str, Any],
    *,
    current: str = "",
    platform_tag: str | None = None,
    shape: str | None = None,
) -> UpdateInfo:
    """
    Interpret a GitHub release payload (or a build ``manifest.json``).

    Malformed pieces never raise: a missing tag yields an empty ``latest``,
    non-object assets are skipped and an unknown digest becomes ``None``.
    The result is only ``available`` when the tag parses as a newer version.

    :param payload: a decoded release payload.
    :param current: the running version, used for the comparison.
    :param platform_tag: override for :func:`detect_platform`.
    :return: the parsed :class:`UpdateInfo`.
    """
    # Tags and URLs are remote data: only strings are trusted, anything else
    # is treated as absent rather than stringified into a bogus version.
    tag = payload.get("tag_name") or payload.get("version")
    latest = tag.strip() if isinstance(tag, str) else ""
    if latest[:1] in ("v", "V"):
        latest = latest[1:]

    notes = payload.get("html_url") or payload.get("notes_url")
    notes_url: str | None = None
    if isinstance(notes, str):
        notes_url = notes.strip() or None

    raw_assets = payload.get("assets")
    assets: list[ReleaseAsset] = []
    if isinstance(raw_assets, (list, tuple)):
        for item in raw_assets:
            asset = _asset_from_payload(item)
            if asset is not None:
                assets.append(asset)
    frozen = tuple(assets)

    tag_platform = platform_tag if platform_tag else detect_platform()
    asset = select_asset(frozen, tag_platform, shape=shape) if tag_platform else None

    return UpdateInfo(
        current=str(current or ""),
        latest=latest,
        available=bool(latest) and is_newer(latest, current),
        notes_url=notes_url,
        asset=asset,
        assets=frozen,
    )


# ---------------------------------------------------------------------------
# checksums and mirrors
# ---------------------------------------------------------------------------


def verify_sha256(path: Path, expected: str) -> bool:
    """
    Whether the file at ``path`` has the expected SHA-256 digest.

    The file is streamed in chunks, never loaded into memory, and the final
    comparison uses :func:`hmac.compare_digest`.  A missing/unreadable file or
    a malformed ``expected`` digest simply returns ``False``.

    :param path: file to hash.
    :param expected: hex digest, with or without a ``sha256:`` prefix.
    :return: ``True`` on an exact match.
    """
    wanted = _normalise_sha(expected)
    if wanted is None:
        return False
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError:
        return False
    return hmac.compare_digest(digest.hexdigest(), wanted)


def _is_http_url(url: str) -> bool:
    """Whether ``url`` starts with an accepted scheme."""
    return str(url or "").strip().lower().startswith(("http://", "https://"))


def _mirror_url(prefix: str, url: str) -> str:
    """
    Build one mirror candidate from ``prefix`` and the original ``url``.

    A missing trailing slash is added, otherwise ``https://proxy.example`` and
    ``https://github.com/...`` would be glued together into the single (invalid)
    token ``https://proxy.examplehttps://github.com/...``.  A prefix that
    already ends with ``url`` is a complete mirror URL and is used verbatim.

    :param prefix: mirror prefix, e.g. ``"https://proxy.example"``.
    :param url: the original absolute asset URL.
    :return: the candidate URL, or ``""`` for an empty prefix.
    """
    base = str(prefix or "").strip()
    if not base:
        return ""
    if base.endswith(url):
        return base
    if not base.endswith("/"):
        base = f"{base}/"
    return f"{base}{url}"


def _head_ok(url: str, timeout: float) -> bool:
    """Whether a ``HEAD`` request to ``url`` succeeds (2xx/3xx)."""
    try:
        request = urllib.request.Request(  # noqa: S310 - caller-validated URL
            url, method="HEAD", headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
    except Exception:  # noqa: BLE001 - an unreachable mirror is not an error
        return False
    return 200 <= status < 400


def resolve_mirror(
    url: str,
    mirrors: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """
    Return the first mirror-prefixed URL that answers, else ``url``.

    Each candidate is ``prefix + url`` (a missing trailing slash on the prefix
    is added first); mirrors are tried in order and a non-HTTP(S) candidate is
    ignored.  When every mirror fails the original URL is returned unchanged,
    so the caller still has a usable fallback.

    :param url: the original, absolute asset URL.
    :param mirrors: proxy prefixes, e.g. ``["https://ghproxy.example/"]``.
    :param timeout: per-mirror probe timeout in seconds; a dead mirror costs
        up to one ``timeout`` and the whole probe up to ``len(mirrors)`` of
        them, so callers should keep prefixes few and the timeout short.
    :return: the chosen URL.
    """
    if not _is_http_url(url):
        return url
    for prefix in mirrors:
        candidate = _mirror_url(prefix, url)
        if not _is_http_url(candidate):
            continue
        if _head_ok(candidate, timeout):
            return candidate
    return url


# ---------------------------------------------------------------------------
# download (no execution)
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Reduce ``name`` to a safe, single path component."""
    base = Path(str(name or "").replace("\\", "/")).name
    cleaned = _UNSAFE_FILENAME_RE.sub("_", base).strip("._")
    cleaned = cleaned[:_MAX_FILENAME].strip("._")
    if cleaned.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
        # ``CON``/``NUL`` address devices, not files, even with an extension:
        # writing to them fails or hangs, so shift the name off the device.
        cleaned = f"_{cleaned}"
    return cleaned or "download.bin"


def _normalise_mirrors(mirror: str | Sequence[str] | None) -> list[str]:
    """Turn the ``mirror`` argument into a list of prefixes."""
    if mirror is None:
        return []
    if isinstance(mirror, str):
        return [mirror]
    return [str(item) for item in mirror]


class _StalePartial(Exception):
    """Internal: the on-disk ``.part`` cannot be resumed, restart without it."""


_CONTENT_RANGE_RE = re.compile(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", re.IGNORECASE)


def _content_range_start(headers: Any) -> int | None:
    """
    The first byte position a ``206`` response claims to carry.

    ``None`` when the header is absent or malformed.  The start is what makes
    a partial response safe to append: without it, a body from an unrelated
    offset would silently corrupt the file.
    """
    value = headers.get("Content-Range") if headers is not None else None
    if not isinstance(value, str):
        return None
    match = _CONTENT_RANGE_RE.fullmatch(value)
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        return None
    return start


def _free_disk_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding *path*, or ``None`` if unknown."""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def download_asset(
    url: str,
    dest: Path,
    expected_sha256: str | None = None,
    mirror: str | Sequence[str] | None = None,
    *,
    root: Path | None = None,
    timeout: float = 30.0,
    resume: bool = False,
    progress: Callable[[int, int], None] | None = None,
    attempts: int = 1,
    max_bytes: int | None = None,
) -> Path:
    """
    Download one release asset to ``dest`` and verify its checksum.

    Nothing is executed, unpacked or launched: the returned path is simply a
    file on disk for the user (or a later feature) to act on.

    Hard rules:

    * only ``http://`` / ``https://`` URLs are accepted;
    * the written file must stay inside ``root`` (default: the current
      directory), so a crafted asset name cannot escape the app home;
    * the target name is sanitised to a single safe path component;
    * a checksum mismatch deletes the partial file and raises.

    :param url: absolute asset URL.
    :param dest: desired output path; a relative path is taken relative to
        ``root`` and its parent and name are normalised.
    :param expected_sha256: digest to verify, when the release provides one.
    :param mirror: optional prefix (or prefixes) tried before ``url``.
    :param root: directory the output must stay inside.
    :param timeout: request timeout in seconds.  Mirror probes are capped at
        :data:`DEFAULT_TIMEOUT` so a dead proxy cannot stall the transfer.
    :param resume: reuse an existing ``.part`` file and continue with an HTTP
        ``Range`` request.  The updater enables this: a 60 MB installer often
        dies halfway on a flaky link, and re-downloading from zero wastes the
        whole transfer.
    :param progress: optional ``(received, total)`` callback; ``total`` is 0
        when the server does not report a length.
    :param attempts: how many times a failed transfer is retried.  With
        ``resume`` the retry continues where the previous one stopped.
    :param max_bytes: refusal threshold for the transfer (default 1 GiB).  A
        manifest is remote data: without a bound, a crafted ``size``/body
        could fill the disk.
    :return: the path actually written.
    :raises ValueError: for a non-HTTP URL, an escaping path or a bad digest.
    :raises OSError: when the transfer itself fails after every attempt.
    """
    if not _is_http_url(url):
        raise ValueError(f"refusing non-HTTP(S) update URL: {url!r}")

    wanted = Path(str(dest))
    root_dir = (Path(root) if root is not None else Path.cwd()).resolve()
    target_dir = wanted.parent
    if not wanted.is_absolute():
        target_dir = root_dir / target_dir
    filename = _sanitize_filename(wanted.name)
    final = (target_dir / filename).resolve()
    if not final.is_relative_to(root_dir):
        raise ValueError(f"refusing to write outside {root_dir}: {final}")
    if final.is_dir():
        raise ValueError(f"refusing to overwrite a directory: {final}")

    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(f"{final.name}.part")
    resolved_url = resolve_mirror(
        url,
        _normalise_mirrors(mirror),
        timeout=min(timeout, DEFAULT_TIMEOUT),
    )

    def report(received: int, total: int) -> None:
        if progress is None:
            return
        with contextlib.suppress(Exception):
            progress(received, total)

    limit = _MAX_ASSET_BYTES if max_bytes is None else max(1, int(max_bytes))
    tries = max(1, int(attempts))
    last_error: BaseException | None = None
    restart_budget = 1
    attempt = 0
    while attempt < tries:
        attempt += 1
        offset = 0
        if resume and part.is_file():
            offset = max(0, part.stat().st_size)
        if offset > limit:
            # A leftover larger than the whole asset can only be stale.
            part.unlink(missing_ok=True)
            offset = 0
        headers = {"User-Agent": _USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(  # noqa: S310 - validated above
                resolved_url, headers=headers
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                # A redirect may downgrade HTTPS to plain HTTP; the digest
                # still protects integrity, but not confidentiality.
                final_url = ""
                getter = getattr(response, "geturl", None)
                if callable(getter):
                    final_url = str(getter() or "")
                if (
                    resolved_url.lower().startswith("https://")
                    and final_url
                    and not final_url.lower().startswith("https://")
                ):
                    raise ValueError(
                        "refusing an HTTPS download that redirected to plain HTTP"
                    )
                status = int(getattr(response, "status", 0) or 0)
                if status == 416:
                    # The server rejected the requested offset outright.
                    raise _StalePartial
                resp_headers = getattr(response, "headers", None)
                appending = False
                if offset > 0 and status == 206:
                    # Only append when the server confirms where this body
                    # starts.  A 206 without ``Content-Range`` (or with a
                    # different start) is not resumable; a 200 with the full
                    # body restarts below.
                    appending = _content_range_start(resp_headers) == offset
                    if not appending:
                        raise _StalePartial
                if not appending:
                    offset = 0
                length = (
                    resp_headers.get("Content-Length")
                    if resp_headers is not None
                    else None
                )
                try:
                    total = offset + int(length) if length else 0
                except (TypeError, ValueError):
                    total = 0
                if total > limit:
                    raise ValueError(
                        f"{filename} is larger than the {limit} byte limit"
                    )
                if total > 0 and offset == 0:
                    # Only refuse a fresh transfer: an existing ``.part`` is
                    # worth keeping for a resume once space is freed.
                    free = _free_disk_bytes(final.parent)
                    if free is not None and total > free:
                        raise ValueError(
                            f"not enough disk space for {filename}: "
                            f"{total} bytes needed, {free} available"
                        )
                received = offset
                report(received, total)
                with part.open("ab" if appending else "wb") as handle:
                    while True:
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > limit:
                            raise ValueError(
                                f"{filename} is larger than the {limit} byte limit"
                            )
                        handle.write(chunk)
                        report(received, total)
                if total and received != total:
                    # The stream ended early (a clean close after a partial
                    # body): promote nothing, keep the bytes for a resume.
                    raise OSError(
                        f"{filename}: transfer ended after {received} of {total} bytes"
                    )
            if expected_sha256 and not verify_sha256(part, expected_sha256):
                # A stale partial from an older release can make a resumed
                # transfer fail its digest; it is useless either way.
                part.unlink(missing_ok=True)
                raise ValueError(f"checksum mismatch for {filename}")
            part.replace(final)
            return final
        except _StalePartial:
            # Not a policy refusal: the prefix on disk is unusable, so drop it
            # and retry from zero once (a 416 or a wrong Content-Range would
            # otherwise fail forever until the stale file is pruned).
            part.unlink(missing_ok=True)
            last_error = OSError(f"cannot resume {filename}")
            if restart_budget <= 0:
                break
            restart_budget -= 1
            attempt -= 1
            continue
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and restart_budget:
                # ``urlopen`` raises before handing us a response object.
                part.unlink(missing_ok=True)
                restart_budget -= 1
                last_error = exc
                attempt -= 1
                continue
            last_error = exc
            if attempt >= tries:
                break
            if not resume:
                part.unlink(missing_ok=True)
            time.sleep(min(5.0, 1.5 * attempt))
        except ValueError:
            # Policy refusals (bad digest, oversized body, HTTPS->HTTP
            # downgrade) are deterministic; retrying cannot help and the
            # partial cannot be verified either.
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            if attempt >= tries:
                break
            if not resume:
                part.unlink(missing_ok=True)
            # Keep a verified prefix for the next attempt; a digest failure
            # already deleted it above.
            time.sleep(min(5.0, 1.5 * attempt))
    assert last_error is not None
    raise OSError(
        f"update download failed after {tries} attempt(s): {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# remote check + throttle cache
# ---------------------------------------------------------------------------

#: Release feed used by every client when no repository is configured.
DEFAULT_REPO = "lingyi-ling/novel-downloader"

#: How often an automatic check may run, in seconds (24 h).
DEFAULT_CHECK_INTERVAL = 24 * 3600

#: Upper bound on the release document we are willing to download.  A hostile
#: or broken mirror could otherwise stream forever.
_MAX_API_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UpdateCheck:
    """Outcome of one update check, including the failure case."""

    info: UpdateInfo | None
    error: str | None
    checked_at: float
    #: ``"manifest"`` when the small published document was used, ``"api"``
    #: when the client had to fall back to the GitHub release API.
    source: str = "api"

    @property
    def available(self) -> bool:
        """Whether a newer release was found."""
        return bool(self.info and self.info.available)


def release_api_url(slug: str = DEFAULT_REPO) -> str:
    """The ``releases/latest`` endpoint for *slug*."""
    cleaned = slug.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cleaned):
        raise ValueError(f"not an owner/name repository slug: {slug!r}")
    return f"https://api.github.com/repos/{cleaned}/releases/latest"


def _fetch_json(url: str, timeout: float) -> Any:
    """GET *url* and decode JSON, reading at most :data:`_MAX_API_BYTES`."""
    request = urllib.request.Request(  # noqa: S310 - https URL built above
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        # The manifest is the updater's trust anchor: never accept one that a
        # redirect fetched over plain HTTP, where it could have been rewritten.
        getter = getattr(response, "geturl", None)
        final_url = str(getter() or "") if callable(getter) else ""
        if final_url and not final_url.lower().startswith("https://"):
            raise ValueError(
                "refusing a release document that redirected to plain HTTP"
            )
        raw = response.read(_MAX_API_BYTES + 1)
    if len(raw) > _MAX_API_BYTES:
        raise ValueError("release document is unexpectedly large")
    return json.loads(raw.decode("utf-8"))


def fetch_latest_release(
    slug: str = DEFAULT_REPO,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[[str, float], Any] | None = None,
) -> dict[str, Any] | None:
    """
    Fetch the latest release document, returning ``None`` on any failure.

    An update check must never break the command that triggered it: offline
    machines, rate limits and malformed payloads all collapse to ``None``.
    """
    try:
        payload = (opener or _fetch_json)(release_api_url(slug), timeout)
    except Exception:  # noqa: BLE001 - a failed check is not an error path
        return None
    return payload if isinstance(payload, dict) else None


def check_for_update(
    current: str,
    *,
    slug: str = DEFAULT_REPO,
    platform_tag: str | None = None,
    shape: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[[str, float], Any] | None = None,
    now: float | None = None,
) -> UpdateCheck:
    """
    Check the release feed once and interpret it for *current*.

    The manifest is preferred: one small request, checksums included.  The API
    is the fallback for releases published before the manifest existed -- but
    **only while no signing key is trusted**.  Once a key is configured the
    manifest is the sole trust anchor: an attacker who controls the repository
    could otherwise delete the manifest (or strip its signature) and force the
    API path, which they also control.
    """
    stamp = time.time() if now is None else now
    pinned = bool(TRUSTED_UPDATE_KEYS)

    manifest = fetch_update_manifest(slug, timeout=timeout, opener=opener)
    if manifest is not None:
        try:
            info = parse_update_manifest(
                manifest,
                current=current,
                platform_tag=platform_tag,
                shape=shape,
                slug=slug,
            )
        except ValueError as exc:
            if pinned:
                return UpdateCheck(
                    info=None, error=f"untrusted-manifest: {exc}", checked_at=stamp
                )
            info = None
        if info is not None:
            return UpdateCheck(
                info=info, error=None, checked_at=stamp, source="manifest"
            )
    elif pinned:
        # No manifest at all while a key is trusted: refuse to consult the
        # API, otherwise removing one file disables signature checking.
        return UpdateCheck(info=None, error="manifest-unavailable", checked_at=stamp)

    payload = fetch_latest_release(slug, timeout=timeout, opener=opener)
    if payload is None:
        return UpdateCheck(info=None, error="unreachable", checked_at=stamp)
    try:
        info = parse_manifest(
            payload, current=current, platform_tag=platform_tag, shape=shape
        )
    except Exception:  # noqa: BLE001 - parse_manifest is defensive already
        return UpdateCheck(info=None, error="malformed", checked_at=stamp)
    return UpdateCheck(info=info, error=None, checked_at=stamp, source="api")


def load_update_state(path: Path) -> dict[str, Any]:
    """Read the throttle file; a missing or corrupt file reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_update_state(
    path: Path, info: UpdateInfo | None, *, now: float | None = None
) -> None:
    """Record that a check ran, atomically; failures are ignored."""
    stamp = time.time() if now is None else now
    payload = {
        "checked_at": stamp,
        "latest": info.latest if info else "",
    }
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(target)
    except OSError:
        return


def check_due(
    path: Path,
    *,
    interval: float = DEFAULT_CHECK_INTERVAL,
    now: float | None = None,
) -> bool:
    """Whether an automatic check should run, per the throttle file."""
    state = load_update_state(path)
    if "checked_at" not in state:
        # No usable record (first run, deleted file, corrupt JSON): a missing
        # timestamp means "never checked", not "checked at the epoch".
        return True
    try:
        last = float(state["checked_at"])
    except (TypeError, ValueError):
        return True
    stamp = time.time() if now is None else now
    return (stamp - last) >= interval


# ---------------------------------------------------------------------------
# updater manifest (small, stable document published with every release)
# ---------------------------------------------------------------------------

#: Where the manifest for the latest release lives.  Unlike the API this URL
#: never changes and is not rate limited.
_MANIFEST_URL = "https://github.com/{slug}/releases/latest/download/latest.json"

#: The manifest schema this client understands.
MANIFEST_SCHEMA = 1

#: Lookup order for a platform: the shape the app was installed from wins.
_PLATFORM_SHAPES: tuple[str, ...] = ("installer", "portable", "cli", "api")


def manifest_url(slug: str = DEFAULT_REPO) -> str:
    """The stable manifest endpoint for *slug*."""
    cleaned = slug.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cleaned):
        raise ValueError(f"not an owner/name repository slug: {slug!r}")
    return _MANIFEST_URL.format(slug=cleaned)


def _manifest_asset(entry: Any) -> ReleaseAsset | None:
    """Decode one ``platforms`` entry, or ``None`` when it is unusable."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    url = entry.get("url")
    digest = entry.get("sha256")
    size = entry.get("size")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    # A manifest advertises files, never paths: a name with a separator could
    # otherwise disagree with the URL it claims to describe.
    if any(char in name for char in ("/", "\\", "\x00")):
        return None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest.strip().lower()):
        return None
    size_value: int | None = None
    if isinstance(size, int) and not isinstance(size, bool) and size > 0:
        if size > _MAX_ASSET_BYTES:
            # The transfer would be refused anyway; do not advertise it.
            return None
        size_value = size
    return ReleaseAsset(
        name=name,
        url=url,
        size=size_value,
        sha256=digest.strip().lower(),
    )


def _manifest_key_shape(key: str) -> str | None:
    """The package shape named by a manifest platform key, if any."""
    lowered = key.strip().lower()
    for shape in ("installer", "portable", "cli", "api"):
        if lowered.endswith(f"-{shape}"):
            return shape
    return None


def _asset_matches_key(key: str, name: str) -> bool:
    """
    Whether *name* is the kind of file a ``platforms`` *key* describes.

    The producer derives the key from the asset name, so the two must agree:
    a ``windows-x64-installer`` entry naming an APK (or an ``android`` entry
    naming a desktop installer) is either a producer bug or tampering, and in
    both cases the client must not install it.
    """
    key_l = key.strip().lower()
    name_l = name.strip().lower()
    if not key_l or not name_l:
        return False
    if key_l == "android" or key_l.startswith("android-"):
        if not name_l.endswith(".apk") or not name_l.startswith(_PROJECT_PREFIXES):
            return False
        abi = key_l[len("android-") :] if key_l.startswith("android-") else ""
        return not abi or abi in name_l
    shape = _manifest_key_shape(key_l)
    if shape == "installer":
        return key_l.startswith("windows-") and name_l.endswith(".exe")
    if shape in {"portable", "cli", "api"}:
        platform_tag = key_l[: -(len(shape) + 1)]
        prefixes = {
            "portable": _PROJECT_PREFIXES,
            "cli": ("novel-cli-",),
            "api": ("novel-api-",),
        }[shape]
        # The platform tag in the key must appear in the filename, so a
        # ``windows-x64`` entry can never offer the Linux or ARM build.
        return (
            bool(platform_tag)
            and name_l.startswith(prefixes)
            and name_l.endswith(f"-{platform_tag}.zip")
        )
    # A bare ``<os>-<arch>`` key (older manifests): accept an archive or an
    # installer, never an APK on a desktop target.
    return name_l.endswith((".zip", ".exe"))


def _manifest_url_problem(
    url: str, *, slug: str | None, tag: str | None, name: str
) -> str | None:
    """
    Why *url* is not a release download URL for *slug*/*tag*/*name*, if so.

    Clients either download or execute what the manifest points at, so an
    entry must resolve to an asset published by the same repository under the
    same tag.  Anything else (another host, repository, tag or filename) is
    rejected instead of being silently preferred.  ``slug``/``tag`` may be
    ``None`` when the manifest does not name them; the structural checks and
    the filename binding still apply.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return "not an https url"
    if parts.query or parts.fragment:
        return "unexpected query or fragment"
    if (parts.hostname or "").lower() != "github.com":
        return "not hosted on github.com"
    if parts.port not in (None, 443):
        return "unexpected port"
    segments = parts.path.split("/")
    if len(segments) != 7 or segments[0] != "":
        return "not a release download url"
    owner, repo, kind, action, raw_tag, raw_name = segments[1:]
    if kind != "releases" or action != "download":
        return "not a release download url"
    if slug is not None:
        slug_owner, _, slug_repo = slug.partition("/")
        if owner.lower() != slug_owner.lower() or repo.lower() != slug_repo.lower():
            return f"not a {slug} release"
    if tag is not None and unquote(raw_tag) != tag:
        return "not the advertised tag"
    if unquote(raw_name) != name:
        return "not the advertised asset"
    return None


def select_manifest_asset(
    platforms: Mapping[str, Any],
    platform_tag: str,
    *,
    shape: str | None = None,
) -> ReleaseAsset | None:
    """
    Pick the entry for *platform_tag* from a manifest's ``platforms`` map.

    The manifest keys are ``<platform>[-<shape>]``; a client installed from an
    installer must update to the installer, not to the portable archive, so
    the shapes are tried in a fixed order.  An entry whose filename does not
    match its key's shape (an APK under a desktop key, say) is ignored.
    """
    tag = platform_tag.strip().lower()
    requested = str(shape or "").strip().lower()
    order = _PLATFORM_SHAPES
    if requested in _PLATFORM_SHAPES:
        order = (requested,) + tuple(
            item for item in _PLATFORM_SHAPES if item != requested
        )
    candidates = [f"{tag}-{item}" for item in order]
    candidates.append(tag)
    if tag.startswith("android"):
        # Split APKs use ``android-<abi>`` keys instead of a package shape;
        # the canonical bare key stays first but a per-ABI entry is usable.
        candidates.extend(
            key
            for key in platforms
            if isinstance(key, str) and key.strip().lower().startswith("android-")
        )
    for key in candidates:
        asset = _manifest_asset(platforms.get(key))
        if asset is None or not _asset_matches_key(key, asset.name):
            continue
        return asset
    return None


def parse_update_manifest(
    payload: dict[str, Any],
    *,
    current: str = "",
    platform_tag: str | None = None,
    shape: str | None = None,
    slug: str | None = None,
) -> UpdateInfo:
    """
    Interpret an updater manifest, raising :class:`ValueError` when unusable.

    A manifest from an unknown schema is rejected rather than guessed at: a
    future format may move the digest, and silently reading it the old way
    would defeat the checksum.  When *slug* is given (the repository the
    document was fetched for) the manifest must name that repository, its tag
    must agree with its version, and every asset URL must be an HTTPS
    ``releases/download/<tag>/<name>`` URL of that repository.  Without those
    bindings a manifest served from anywhere could point the updater at an
    unrelated executable and its matching digest.
    """
    verify_manifest_signature(payload)

    schema = payload.get("schema")
    if schema != MANIFEST_SCHEMA:
        raise ValueError(f"unsupported manifest schema: {schema!r}")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest has no version")
    latest = version.strip()
    if latest[:1] in ("v", "V"):
        latest = latest[1:]

    raw_tag = payload.get("tag")
    tag = raw_tag.strip() if isinstance(raw_tag, str) else ""
    if tag:
        if tag.lstrip("vV") != latest:
            raise ValueError(f"manifest tag {tag!r} does not match version {latest!r}")
    elif slug is not None:
        raise ValueError("manifest has no tag")

    raw_repo = payload.get("repo")
    repo = raw_repo.strip() if isinstance(raw_repo, str) else ""
    if slug is not None:
        expected = slug.strip().strip("/")
        if not repo or repo.lower() != expected.lower():
            raise ValueError(
                f"manifest repository {repo or '<none>'!r} does not match {expected!r}"
            )

    platforms = payload.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ValueError("manifest has no platforms")

    # Validate every advertised payload, not only the one this platform will
    # use: a manifest that points outside its own release is not trustworthy.
    for key, entry in platforms.items():
        advertised = _manifest_asset(entry)
        if advertised is None:
            # Structurally unusable entries are simply never selected.
            continue
        if not isinstance(key, str) or not _asset_matches_key(key, advertised.name):
            raise ValueError(
                f"manifest platform {key!r} does not describe {advertised.name!r}"
            )
        problem = _manifest_url_problem(
            advertised.url,
            slug=repo or None,
            tag=tag or None,
            name=advertised.name,
        )
        if problem is not None:
            raise ValueError(
                f"manifest platform {key!r} has an untrusted url: {problem}"
            )

    tag_platform = platform_tag if platform_tag else detect_platform()
    asset = (
        select_manifest_asset(platforms, tag_platform, shape=shape)
        if tag_platform
        else None
    )

    notes = payload.get("notes_url")
    notes_url = notes.strip() if isinstance(notes, str) and notes.strip() else None

    return UpdateInfo(
        current=str(current or ""),
        latest=latest,
        available=is_newer(latest, current),
        notes_url=notes_url,
        asset=asset,
        assets=(asset,) if asset is not None else (),
    )


def fetch_update_manifest(
    slug: str = DEFAULT_REPO,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[[str, float], Any] | None = None,
) -> dict[str, Any] | None:
    """Fetch the manifest, returning ``None`` on any failure."""
    try:
        payload = (opener or _fetch_json)(manifest_url(slug), timeout)
    except Exception:  # noqa: BLE001 - an update check must never raise
        return None
    return payload if isinstance(payload, dict) else None


#: Public keys (``key_id`` -> hex) whose manifest signatures are trusted.
#:
#: Empty by default: manifests are then accepted unsigned, exactly like before
#: signatures existed.  Once a key is added here, an unsigned or wrongly
#: signed manifest is rejected outright -- otherwise an attacker who controls
#: the repository could simply strip the signature and fall back to the API.
TRUSTED_UPDATE_KEYS: Mapping[str, str] = {}


def _manifest_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical bytes a signature covers (mirrors the build script)."""
    body = {k: v for k, v in payload.items() if k not in {"signature", "key_id"}}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_manifest_signature(payload: Mapping[str, Any]) -> None:
    """
    Check a manifest signature when trusted keys are configured.

    :raises ValueError: when a key is configured and the signature is missing,
        from an unknown key, or does not verify.
    """
    if not TRUSTED_UPDATE_KEYS:
        return

    key_id = payload.get("key_id")
    signature = payload.get("signature")
    if not isinstance(key_id, str) or not isinstance(signature, str):
        raise ValueError("manifest is not signed but a trusted key is configured")
    public_hex = TRUSTED_UPDATE_KEYS.get(key_id)
    if public_hex is None:
        raise ValueError(f"manifest is signed with an unknown key: {key_id!r}")

    try:
        from Crypto.Signature import eddsa

        raw = base64.b64decode(signature, validate=True)
        public = eddsa.import_public_key(bytes.fromhex(public_hex))
        eddsa.new(public, "rfc8032").verify(_manifest_signing_bytes(payload), raw)
    except Exception as exc:  # noqa: BLE001 - any failure is a rejection
        raise ValueError(f"manifest signature does not verify: {exc}") from exc
