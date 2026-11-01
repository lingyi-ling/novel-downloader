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

import hashlib
import hmac
import json
import platform
import re
import shutil
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_CHECK_INTERVAL",
    "DEFAULT_REPO",
    "DEFAULT_TIMEOUT",
    "ReleaseAsset",
    "UpdateCheck",
    "UpdateInfo",
    "check_due",
    "check_for_update",
    "fetch_latest_release",
    "load_update_state",
    "release_api_url",
    "save_update_state",
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
    ``"v1.10.0-rc1"`` becomes ``(1, 10, 0)``.  Unparseable input yields
    ``(0,)`` instead of raising, because release tags are remote data.

    :param text: a version string, e.g. ``"v1.2.3"``.
    :return: a tuple of integers, never empty.
    """
    head = str(text or "").strip().split("-")[0].lstrip("vV")
    parts: list[int] = []
    for chunk in head.split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """
    Whether ``latest`` is a strictly newer version than ``current``.

    Missing trailing components count as zero, so ``1.2.3.0`` and ``1.2.3``
    compare equal rather than one being "newer".

    :param latest: the candidate version.
    :param current: the version in use.
    :return: ``True`` only when ``latest`` > ``current``.
    """
    left = parse_version(latest)
    right = parse_version(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


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
    assets: Sequence[ReleaseAsset], platform_tag: str
) -> ReleaseAsset | None:
    """
    Pick the release asset that belongs to ``platform_tag``.

    Preference order, using the project's naming rules:

    * Android: the single ``novel-downloader-<ver>-android.apk``, or the first
      project APK when the release publishes per-ABI names;
    * Windows x64: the ``NovelDownloader-setup-<ver>-x64.exe`` installer,
      falling back to the desktop bundle zip;
    * everything else: the ``NovelDownloader-<ver>-<platform>.zip`` bundle.

    CLI/API bundles are never returned: they are not what the desktop client
    would install.  When nothing matches, ``None`` is returned rather than a
    guess.

    :param assets: every asset of the release, in payload order.
    :param platform_tag: a tag as produced by :func:`detect_platform`.
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
        if installer is not None:
            return installer
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
    asset = select_asset(frozen, tag_platform) if tag_platform else None

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


def download_asset(
    url: str,
    dest: Path,
    expected_sha256: str | None = None,
    mirror: str | Sequence[str] | None = None,
    *,
    root: Path | None = None,
    timeout: float = 30.0,
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
    :return: the path actually written.
    :raises ValueError: for a non-HTTP URL, an escaping path or a bad digest.
    :raises OSError: when the transfer itself fails.
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

    try:
        request = urllib.request.Request(  # noqa: S310 - validated above
            resolved_url, headers={"User-Agent": _USER_AGENT}
        )
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,  # noqa: S310
            part.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, _CHUNK_SIZE)
    except Exception:
        part.unlink(missing_ok=True)
        raise

    if expected_sha256 and not verify_sha256(part, expected_sha256):
        part.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch for {filename}")

    part.replace(final)
    return final


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
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[[str, float], Any] | None = None,
    now: float | None = None,
) -> UpdateCheck:
    """Check the release feed once and interpret it for *current*."""
    stamp = time.time() if now is None else now
    payload = fetch_latest_release(slug, timeout=timeout, opener=opener)
    if payload is None:
        return UpdateCheck(info=None, error="unreachable", checked_at=stamp)
    try:
        info = parse_manifest(payload, current=current, platform_tag=platform_tag)
    except Exception:  # noqa: BLE001 - parse_manifest is defensive already
        return UpdateCheck(info=None, error="malformed", checked_at=stamp)
    return UpdateCheck(info=info, error=None, checked_at=stamp)


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
