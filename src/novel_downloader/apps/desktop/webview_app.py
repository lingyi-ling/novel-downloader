#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.webview_app
-----------------------------------------

The pywebview host for the desktop client.

A single :class:`ApiBridge` instance is exposed to the embedded front end as
``window.pywebview.api``.  Every public method must accept and return
JSON-serialisable values because pywebview serialises them across the JS
bridge; long-running work happens on dedicated threads so the UI stays
responsive:

* task-manager coroutines run on :class:`AsyncEngine`'s persistent background
  event loop;
* music downloads and platform health checks run on their own daemon threads
  and report progress through polled state dictionaries.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES, SEARCH_SUPPORT_SITES
from novel_downloader.apps.desktop.formatting import format_error, status_label
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.infra.i18n import t

if TYPE_CHECKING:
    from novel_downloader.apps.common.updater import UpdateInfo
    from novel_downloader.apps.desktop.engine import AsyncEngine

__all__ = ["ApiBridge", "run_webview_app"]

logger = logging.getLogger(__name__)


def _updater() -> Any:
    """
    Import the update helpers on first use.

    ``novel_downloader.apps.common.updater`` pulls in ``urllib.request`` (and
    through it ``http.client``), which nothing on the window-creation path
    needs: update checks run after the page is ready.  Keeping the import here
    takes that stack out of the cold-start critical path.
    """
    from novel_downloader.apps.common import updater

    return updater


# The names below re-export the updater helpers lazily.  They are ordinary
# module-level functions (not attribute aliases) so tests and embedders can
# keep monkeypatching ``webview_app.download_asset`` and friends while the
# actual import still happens on first use.


def check_for_update(*args: Any, **kwargs: Any) -> Any:
    """Deferred :func:`novel_downloader.apps.common.updater.check_for_update`."""
    from novel_downloader.apps.common.updater import check_for_update as impl

    return impl(*args, **kwargs)


def detect_install_shape() -> str:
    """Deferred :func:`updater.detect_install_shape`."""
    from novel_downloader.apps.common.updater import detect_install_shape as impl

    return impl()


def detect_platform() -> str:
    """Deferred :func:`updater.detect_platform`."""
    from novel_downloader.apps.common.updater import detect_platform as impl

    return impl()


def download_asset(*args: Any, **kwargs: Any) -> Any:
    """Deferred :func:`updater.download_asset`."""
    from novel_downloader.apps.common.updater import download_asset as impl

    return impl(*args, **kwargs)


def parse_manifest(*args: Any, **kwargs: Any) -> Any:
    """Deferred :func:`updater.parse_manifest`."""
    from novel_downloader.apps.common.updater import parse_manifest as impl

    return impl(*args, **kwargs)


def verify_sha256(path: Path, expected: str) -> bool:
    """Deferred :func:`updater.verify_sha256`."""
    from novel_downloader.apps.common.updater import verify_sha256 as impl

    return impl(path, expected)


#: Per-site search timeout handed to :func:`plugins.search.search`.
SEARCH_SITE_TIMEOUT = 6.0
#: Upper bound for awaiting a submitted search on the engine loop.
SEARCH_WAIT_TIMEOUT = 180.0
#: Simultaneous probes while checking every music platform.
HEALTH_WORKERS = 8
#: Budget for one music health probe, in seconds.  A platform that cannot
#: answer a one-result search in five seconds is reported as unhealthy instead
#: of holding the panel open for the old 15 s default.
HEALTH_CHECK_TIMEOUT = 5.0
#: Progressive music searches kept for polling; older ones are evicted first.
MUSIC_SEARCH_LIMIT = 8
#: Upper bound for awaiting task-manager coroutines.
TASK_WAIT_TIMEOUT = 30.0

_PREFS_FILENAME = "desktop_prefs.json"

#: Name of the shortcut files created on the desktop and in the Start menu.
_SHORTCUT_NAME = "书音.lnk"

#: GitHub endpoints used by the in-app update check.
_RELEASES_API = (
    "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/latest"
)
_RELEASES_PAGE = "https://github.com/lingyi-ling/novel-downloader/releases/latest"
#: Minimum interval between update checks (24 hours).
_UPDATE_CHECK_INTERVAL = 24 * 3600.0
#: Network timeout for an update check, in seconds.  Mirrors
#: ``updater.DEFAULT_TIMEOUT``; importing that module here would load
#: ``urllib.request`` before the window exists.
_UPDATE_TIMEOUT = 8.0
#: Directory under :func:`app_home` holding downloaded update files.
_UPDATES_DIRNAME = "updates"
#: PyInstaller's onedir payload directory (pinned by
#: ``packaging/novel_desktop.spec`` and cleaned by ``noveldl_installer.iss``).
_PAYLOAD_DIRNAME = "_internal"
#: Timeout for an asset transfer, in seconds.  Downloads are much larger than
#: the release document, so they get their own (generous) budget.
_UPDATE_DOWNLOAD_TIMEOUT = 300.0
#: Named mutex mirrored by ``AppMutex`` in ``packaging/noveldl_installer.iss``:
#: holding it lets the installer's Restart Manager identify this process.
_UPDATE_MUTEX_NAME = "NovelDownloader-UpdateMutex"
#: Arguments for the Inno Setup installer: no wizard, no message boxes, and a
#: Restart-Manager request to close a running instance.  There is deliberately
#: no ``/RESTARTAPPLICATIONS``: Setup only restarts programs that called
#: ``RegisterApplicationRestart`` (this one does not), and the installer's
#: ``[Run]`` entry already starts the new build after a silent install.
#: Asking for both would launch a second instance.
_INSTALLER_ARGS = (
    "/SILENT",
    "/SUPPRESSMSGBOXES",
    "/CLOSEAPPLICATIONS",
)
#: Delay before closing the window after the installer starts.  Only long
#: enough for the ``install_update`` reply to cross the JS bridge; the process
#: never waits for the installer itself.
_INSTALL_EXIT_DELAY = 0.25
#: Grace period before retrying a window close that raised, so a GUI thread
#: still delivering the bridge reply cannot make us give up too early.
_INSTALL_EXIT_RETRY_DELAY = 1.0
#: Grace period for user data / old update files: ``.part`` leftovers expire
#: after a day, finished downloads after a week.
_PRUNE_PART_AGE = 24 * 3600.0
_PRUNE_FILE_AGE = 7 * 24 * 3600.0
#: Helper that replaces a *portable* copy after the app exits.  PowerShell is
#: shipped with Windows 10/11, so this needs no extra runtime; arguments are
#: always passed as separate argv entries (never parsed from a command line).
_PORTABLE_UPDATE_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][int]$ProcessId,
    [Parameter(Mandatory=$true)][string]$Zip,
    [Parameter(Mandatory=$true)][string]$AppDir,
    [Parameter(Mandatory=$true)][string]$Exe,
    [string[]]$Keep = @()
)
$ErrorActionPreference = 'Stop'

function Test-PathWithin([string]$Path, [string]$Root) {
    if ([string]::IsNullOrWhiteSpace($Root)) { return $false }
    $p = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $r = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($p.Equals($r, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $p.StartsWith($r + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-Protected([string]$Path) {
    foreach ($k in @($Keep)) {
        if ([string]::IsNullOrWhiteSpace($k)) { continue }
        # Keep the user-data tree itself and any payload subtree that contains
        # it: user settings/downloads must never be removed by an update.
        if ((Test-PathWithin $Path $k) -or (Test-PathWithin $k $Path)) { return $true }
    }
    return $false
}

# Wait for the running instance to release its executable and DLLs.
for ($i = 0; $i -lt 240; $i++) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}

$AppDir = [System.IO.Path]::GetFullPath($AppDir)
# A misdetected install shape must never turn an update into "clean a drive".
if ($AppDir -eq [System.IO.Path]::GetPathRoot($AppDir)) {
    throw "Refusing to update a drive root: $AppDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $AppDir $Exe))) {
    throw "The installed executable is missing: $AppDir\$Exe"
}

$temp = Join-Path $env:TEMP ('noveldl-update-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $Zip -DestinationPath $temp -Force
$items = @(Get-ChildItem -LiteralPath $temp)
if ($items.Count -eq 0) { throw "The update archive is empty" }
$source = $temp
if ($items.Count -eq 1 -and $items[0].PSIsContainer) { $source = $items[0].FullName }

# PyInstaller puts every module/resource below _internal.  Overwriting files
# alone leaves removed plugins behind (the registry enumerates them from
# disk), so the old payload directory is cleared first.  User data lives in
# app_home(), never in _internal; the $Keep guard only matters when NOVEL_HOME
# points inside the payload, and then the protected subtree is preserved.
$payload = Join-Path $AppDir '_internal'
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    try {
        if ((Test-Path -LiteralPath $payload) -and -not (Test-Protected $payload)) {
            Remove-Item -LiteralPath $payload -Recurse -Force
        }
        break
    } catch {
        if ($attempt -eq 9) { throw }
        Start-Sleep -Seconds 1
    }
}

# A slow antivirus scan can keep a file locked for a moment; retry briefly.
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    try {
        Copy-Item -Path (Join-Path $source '*') -Destination $AppDir -Recurse -Force
        break
    } catch {
        if ($attempt -eq 9) { throw }
        Start-Sleep -Seconds 1
    }
}
Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
Start-Process -FilePath (Join-Path $AppDir $Exe)
"""


def _version_tuple(value: str | None) -> tuple[int, ...]:
    """
    Parse a release version (pre-existing bridge-test hook).

    The implementation lives in :mod:`apps.common.updater` so the update logic
    has a single source of truth (and the common module stays GUI-free); the
    import is deferred so loading the bridge does not pull in the network stack.
    """
    parsed: tuple[int, ...] = _updater().parse_version(value or "")
    return parsed


#: PowerShell template that writes the .lnk files via the WScript.Shell COM
#: object.  ``{target}`` is the frozen executable path (single quotes doubled).
_SHORTCUT_PS = """\
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$target = '{target}'
$workdir = Split-Path -Parent $target
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
foreach ($dir in @($desktop, $programs)) {{
  $lnk = $ws.CreateShortcut((Join-Path $dir '{name}'))
  $lnk.TargetPath = $target
  $lnk.WorkingDirectory = $workdir
  $lnk.IconLocation = "$target,0"
  $lnk.Description = '书音 BookTone'
  $lnk.Save()
}}
"""


def _shell_folder(csidl: int) -> str | None:
    """Resolve a Windows shell folder path (``None`` elsewhere or on error)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        return buf.value if result == 0 else None
    except Exception:  # noqa: BLE001 - best effort, cosmetic only
        return None


def _shortcut_target() -> str | None:
    """Path of the frozen executable to point shortcuts at, if packaged."""
    if getattr(sys, "frozen", False):
        return str(sys.executable)
    return None


def _virtual_screen_rect() -> tuple[int, int, int, int] | None:
    """
    Bounding rectangle of the whole Windows virtual desktop.

    Monitors laid out left of or above the primary produce negative screen
    coordinates; the rectangle is what makes those restorable instead of
    being clamped onto the primary monitor.

    :return: ``(x, y, width, height)`` in screen coordinates, or ``None``
        when the metrics are unavailable (non-Windows, headless session).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        rect = (
            int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
            int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
            int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
            int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
        )
    except Exception:  # noqa: BLE001 - best effort, cosmetic only
        return None
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect


def _shortcut_paths() -> tuple[Path, ...]:
    """Both locations the shortcut is written to."""
    dirs = (
        _shell_folder(0x0016),  # CSIDL_DESKTOPDIRECTORY
        _shell_folder(0x0002),  # CSIDL_PROGRAMS (Start menu)
    )
    return tuple(Path(d) / _SHORTCUT_NAME for d in dirs if d)


#: Lazily-loaded music engine entry points. The music modules are still
#: untyped, so they are kept behind an ``Any``-valued mapping (the same
#: pattern the previous Tkinter UI used) and imported only when the extras
#: are actually present.
_MUSIC_FUNCS: dict[str, Any] | None = None


def _music_funcs() -> dict[str, Any] | None:
    """Return the music engine entry points, or ``None`` without the extras."""
    global _MUSIC_FUNCS
    if _MUSIC_FUNCS is None:
        try:
            from novel_downloader.apps.music.aggregate import (
                ALL_PLATFORM_KEY,
                is_all_platforms,
                search_all,
            )
            from novel_downloader.apps.music.downloader import (
                download_mixed,
                export_m3u,
            )
            from novel_downloader.apps.music.providers import (
                available_labels,
                create,
                health_check,
                platform_note,
            )
            from novel_downloader.apps.music.providers.base import Track
            from novel_downloader.apps.music.trackcache import CACHE
        except Exception as exc:  # noqa: BLE001 - optional extras missing
            logger.debug("Music extras unavailable: %s", exc)
            _MUSIC_FUNCS = {}
            return None
        _MUSIC_FUNCS = {
            "available_labels": available_labels,
            "create": create,
            "health_check": health_check,
            "platform_note": platform_note,
            "download_mixed": download_mixed,
            "export_m3u": export_m3u,
            "search_all": search_all,
            "is_all_platforms": is_all_platforms,
            "CACHE": CACHE,
            "ALL_PLATFORM_KEY": ALL_PLATFORM_KEY,
            "Track": Track,
        }
    return _MUSIC_FUNCS or None


#: Heavy music platforms that need an account and/or an optional dependency.
#: The provider registry only lists a platform whose dependency imports, so
#: without this table TIDAL/Apple/SoundCloud/YouTube simply disappeared and
#: their failures stayed unexplained. ``credentials`` rows are also what the
#: settings page's "Music accounts" card writes through ``providers.creds``.
_MUSIC_HEAVY_GUIDANCE: dict[str, dict[str, Any]] = {
    "tidal": {
        "label": "TIDAL",
        "dependency": "aigpy",
        "tools": ("N_m3u8DL-RE",),
        "credentials": True,
        "credential_required": True,
    },
    "apple": {
        "label": "Apple Music",
        "dependency": "pywidevine",
        "tools": ("ffmpeg",),
        "credentials": True,
        "credential_required": False,
    },
    "soundcloud": {
        "label": "SoundCloud",
        "dependency": "pywidevine",
        "tools": ("N_m3u8DL-RE",),
        "credentials": True,
        "credential_required": False,
    },
    "youtube": {
        "label": "YouTube",
        "dependency": "ytmusicapi",
        "tools": (),
        "credentials": False,
        "credential_required": False,
    },
}

#: Platforms whose account token can be stored from the settings page.
_MUSIC_CREDENTIAL_KEYS = frozenset(
    key for key, meta in _MUSIC_HEAVY_GUIDANCE.items() if meta.get("credentials")
)


def _music_dependency_available(name: str) -> bool:
    """Whether an optional Python dependency can be imported."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _music_tool_available(name: str) -> bool:
    """Whether an external executable is on ``PATH``."""
    return shutil.which(name) is not None


def _music_credential_configured(key: str) -> bool:
    """Whether a platform has a non-empty credential object (never its value)."""
    try:
        from novel_downloader.apps.music.providers import creds

        data = creds.get(key)  # type: ignore[no-untyped-call]
    except Exception:  # noqa: BLE001 - optional extras / unreadable file
        logger.debug("Could not read the music credentials for %s", key, exc_info=True)
        return False
    return isinstance(data, dict) and bool(data)


def _music_credential_hint(key: str) -> str:
    """Describe the JSON shape expected by one platform's credential blob."""
    if key == "tidal":
        return t(
            "Paste the TIDAL token JSON (access_token, refresh_token, "
            "client_id, client_secret)."
        )
    if key == "apple":
        return t('Paste a cookie JSON with "media-user-token".')
    if key == "soundcloud":
        return t('Paste a token JSON with "oauth_token" and "client_id".')
    return ""


def _music_credential_rows() -> list[dict[str, Any]]:
    """Platform credential rows for the settings card (values never included)."""
    rows: list[dict[str, Any]] = []
    for key, meta in _MUSIC_HEAVY_GUIDANCE.items():
        if not meta.get("credentials"):
            continue
        rows.append(
            {
                "key": key,
                "label": str(meta["label"]),
                "configured": _music_credential_configured(key),
                "required": bool(meta.get("credential_required")),
                "hint": _music_credential_hint(key),
            }
        )
    return rows


def _music_platform_status() -> list[dict[str, Any]]:
    """Guidance rows for the heavy platforms, including unavailable ones.

    The provider registry hides a platform when an optional package is
    missing. The music page needs the opposite: a row that names the package
    or external tool and points at the account settings, so a missing token is
    never rendered as an empty result list.
    """
    funcs = _music_funcs()
    registered: set[str] = set()
    if funcs is not None:
        try:
            registered = {str(key) for key, _label in funcs["available_labels"]()}
        except Exception:  # noqa: BLE001 - guidance must not break the page
            logger.debug("Could not list the registered music platforms", exc_info=True)

    rows: list[dict[str, Any]] = []
    for key, meta in _MUSIC_HEAVY_GUIDANCE.items():
        dependency = str(meta["dependency"])
        dependency_ok = _music_dependency_available(dependency)
        missing_tools = [
            str(tool)
            for tool in meta.get("tools", ())
            if not _music_tool_available(str(tool))
        ]
        configured = (
            _music_credential_configured(key) if meta.get("credentials") else False
        )
        reasons: list[str] = []
        state = "ready"
        if not dependency_ok:
            state = "missing_dependency"
            reasons.append(
                t("Missing optional dependency: {name}").replace("{name}", dependency)
            )
            reasons.append(
                t(
                    "Install the music extras with: "
                    'pip install "novel-downloader[music]"'
                )
            )
        elif funcs is not None and key not in registered:
            state = "unavailable"
            reasons.append(t("This platform is unavailable in this build."))
        if missing_tools:
            if state == "ready":
                state = "missing_tool"
            tools_text = ", ".join(missing_tools)
            reasons.append(
                t("Missing external tool: {name}").replace("{name}", tools_text)
            )
            reasons.append(
                t("Install {name} and add it to your PATH.").replace(
                    "{name}", tools_text
                )
            )
        if meta.get("credentials") and not configured:
            if key == "tidal":
                if state == "ready":
                    state = "needs_credentials"
                reasons.append(
                    t(
                        "TIDAL needs an account token before it can search; paste "
                        "the token JSON under Settings → Music accounts."
                    )
                )
            elif key == "apple":
                if state == "ready":
                    state = "limited"
                reasons.append(
                    t(
                        "Apple Music downloads need a media-user-token cookie; add "
                        "it under Settings → Music accounts. Previews work without it."
                    )
                )
            elif key == "soundcloud":
                if state == "ready":
                    state = "limited"
                reasons.append(
                    t(
                        "SoundCloud downloads need an oauth_token cookie; add it "
                        "under Settings → Music accounts. Search works without it."
                    )
                )
        if funcs is None and state == "ready":
            state = "unavailable"
            reasons.append(t("Music extras are not installed"))
        rows.append(
            {
                "key": key,
                "label": str(meta["label"]),
                "state": state,
                "available": state
                in {"ready", "limited", "needs_credentials", "missing_tool"},
                "needs_credentials": bool(meta.get("credentials")) and not configured,
                "can_configure": bool(meta.get("credentials")),
                "configured": configured,
                "reason": " ".join(part for part in reasons if part),
                "hint": _music_credential_hint(key) if meta.get("credentials") else "",
            }
        )
    return rows


def _music_options_type(**kwargs: Any) -> Any:
    """Construct :class:`DownloadOptions` from the (untyped) music extras."""
    from novel_downloader.apps.music.downloader import DownloadOptions

    return DownloadOptions(**kwargs)


#: Language slots reported for a track's lyric files. These are stable
#: identifiers, not copy: the page localizes the visible label, so a saved
#: result keeps its meaning when the interface language changes.
_LYRIC_LANG_ORIGINAL = "原文"
_LYRIC_LANG_TRANSLATION = "译文"


def _music_lyric_files(audio_path: str) -> tuple[str, str]:
    """
    Return the existing ``(original, translation)`` lyric files for an audio path.

    Both paths are derived from *audio_path* by the music engine
    (``<stem>.lrc`` / ``<stem>.zh.lrc``) and then checked here, so a path from
    an earlier run can never become a dead button. Missing or unsupported
    extras yield ``("", "")``.
    """
    path = str(audio_path or "").strip()
    if not path:
        return "", ""
    try:
        from novel_downloader.apps.music.downloader import lyric_paths

        original, translation = lyric_paths(path)  # type: ignore[no-untyped-call]
    except Exception:  # noqa: BLE001 - optional extras may be absent
        logger.debug("Could not resolve the lyric path for %s", path, exc_info=True)
        return "", ""
    return (
        original if os.path.exists(original) else "",
        translation if os.path.exists(translation) else "",
    )


def _music_lyric_path(audio_path: str) -> str:
    """
    Return the original ``.lrc`` saved next to *audio_path*, or ``""``.

    Kept for compatibility with the page's legacy ``lyric_path`` field: the
    batch summary and the old single "Lyrics" button are built around the
    original file only. Translation-aware callers use
    :func:`_music_lyric_paths`.
    """
    original, _translation = _music_lyric_files(audio_path)
    return original


def _music_lyric_paths(audio_path: str) -> list[dict[str, str]]:
    """
    Return the lyric files saved beside *audio_path*, original first.

    Each entry is ``{"lang": "原文"|"译文", "path": ...}``. ``lang`` is the
    language slot (not translated copy), so the history stays stable if the
    interface language changes; the page renders the localized label. The list
    omits missing files and is empty when neither lyric exists.
    """
    original, translation = _music_lyric_files(audio_path)
    entries: list[dict[str, str]] = []
    if original:
        entries.append({"lang": _LYRIC_LANG_ORIGINAL, "path": original})
    if translation:
        entries.append({"lang": _LYRIC_LANG_TRANSLATION, "path": translation})
    return entries


#: Default suffix for playlists written without an explicit ``out_path``.
_DEFAULT_PLAYLIST_SUFFIX = ".m3u8"


def _default_playlist_name() -> str:
    """Timestamped playlist file name (same convention as the REST API)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"playlist-{stamp}{_DEFAULT_PLAYLIST_SUFFIX}"


def _music_output_dir() -> Path:
    """
    Resolve the directory music downloads are written to.

    Mirrors the engine: the stored ``out_dir`` when the user picked one,
    otherwise the platform's default music directory.  The path is returned
    verbatim (not created) so callers can decide what to do when it is absent.
    """
    from novel_downloader.apps.music.paths import default_download_dir
    from novel_downloader.apps.music.settings import load_settings

    try:
        stored = load_settings()
    except Exception:  # noqa: BLE001 - a broken settings file must not hide the UI
        logger.debug("Could not load music settings", exc_info=True)
        stored = {}
    out_dir = stored.get("out_dir") if isinstance(stored, dict) else None
    if out_dir and str(out_dir).strip():
        return Path(str(out_dir).strip()).expanduser()
    return Path(default_download_dir()).expanduser()


def _playlist_target(out_path: str | None) -> Path:
    """
    Resolve where an exported playlist is written.

    An explicit ``.m3u`` / ``.m3u8`` path is used verbatim, any other value is
    treated as a directory (a typo such as ``songs.txt`` must not silently
    produce a playlist with the wrong extension). Without ``out_path`` the
    music settings' ``out_dir`` is used, falling back to the platform default
    music directory.
    """
    name = _default_playlist_name()
    if out_path and str(out_path).strip():
        candidate = Path(str(out_path).strip()).expanduser()
        if candidate.suffix.lower() in {".m3u", ".m3u8"}:
            return candidate
        return candidate / name
    return _music_output_dir() / name


# ---------------------------------------------------------------------------
# i18n catalogue
# ---------------------------------------------------------------------------


def _i18n_catalog() -> dict[str, str]:
    """
    Collect every front-end string as a literal ``t()`` call.

    The catalogue is injected into the page at boot; keeping the calls literal
    here is what lets the Babel extraction pass discover the msgids.
    """
    return {
        # navigation / chrome
        "Skip to content": t("Skip to content"),
        "Search": t("Search"),
        "Download": t("Download"),
        "History": t("History"),
        "Music": t("Music"),
        "Search in Music": t("Search in Music"),
        "Rankings": t("Rankings"),
        "Categories": t("Categories"),
        "Male": t("Male"),
        "Female": t("Female"),
        "Merge same book": t("Merge same book"),
        "Search to see results": t("Search to see results"),
        "Type a title, or browse the rankings and categories": t(
            "Type a title, or browse the rankings and categories"
        ),
        "Source": t("Source"),
        "Remove": t("Remove"),
        "Follow": t("Follow"),
        "Refresh": t("Refresh"),
        "Open": t("Open"),
        "OK": t("OK"),
        "Cancel": t("Cancel"),
        "Retry": t("Retry"),
        "Stop": t("Stop"),
        "Theme": t("Theme"),
        "Results": t("Results"),
        "Select all": t("Select all"),
        "Add book": t("Add book"),
        "Check updates": t("Check updates"),
        "Open folder": t("Open folder"),
        "Download selected": t("Download selected"),
        "Check all platforms": t("Check all platforms"),
        "Quality": t("Quality"),
        "Quality & tags": t("Quality & tags"),
        "Embed tags": t("Embed tags"),
        "Embed cover": t("Embed cover"),
        "Save lyrics": t("Save lyrics"),
        "Split folders": t("Split folders"),
        "Skip existing": t("Skip existing"),
        "Platform health": t("Platform health"),
        "Ctrl+1-6 switch pages": t("Ctrl+1-6 switch pages"),
        # settings page
        "Settings": t("Settings"),
        "Storage, network, updates and appearance": t(
            "Storage, network, updates and appearance"
        ),
        "Save changes": t("Save changes"),
        "Storage": t("Storage"),
        "Output folder": t("Output folder"),
        "Where downloaded books are saved": t("Where downloaded books are saved"),
        "Output folder path": t("Output folder path"),
        "Folder exists and is writable": t("Folder exists and is writable"),
        "Folder does not exist yet; it will be created when you save": t(
            "Folder does not exist yet; it will be created when you save"
        ),
        "Not a folder": t("Not a folder"),
        "Folder is not writable": t("Folder is not writable"),
        "Network": t("Network"),
        "Proxy": t("Proxy"),
        "Proxy address, e.g. http://127.0.0.1:7890 (blank = direct)": t(
            "Proxy address, e.g. http://127.0.0.1:7890 (blank = direct)"
        ),
        "Proxy username": t("Proxy username"),
        "Proxy password": t("Proxy password"),
        "Saved; leave blank to keep": t("Saved; leave blank to keep"),
        "Optional": t("Optional"),
        "Clear saved password": t("Clear saved password"),
        "Music accounts": t("Music accounts"),
        "Paste account tokens for TIDAL, Apple Music and SoundCloud. They are "
        "stored on this device and never shown again.": t(
            "Paste account tokens for TIDAL, Apple Music and SoundCloud. They are "
            "stored on this device and never shown again."
        ),
        "Platform": t("Platform"),
        "Paste the token JSON": t("Paste the token JSON"),
        "Save credentials": t("Save credentials"),
        "Clear credentials": t("Clear credentials"),
        "Configured": t("Configured"),
        "Not configured": t("Not configured"),
        "Platform credentials saved": t("Platform credentials saved"),
        "Platform credentials cleared": t("Platform credentials cleared"),
        "Could not save the credentials": t("Could not save the credentials"),
        "Could not clear the credentials": t("Could not clear the credentials"),
        "Enter the token JSON before saving": t("Enter the token JSON before saving"),
        "Updates": t("Updates"),
        "Check for updates automatically": t("Check for updates automatically"),
        "Download updates in the background": t("Download updates in the background"),
        "Install the downloaded update when quitting": t(
            "Install the downloaded update when quitting"
        ),
        "Check now": t("Check now"),
        "Appearance": t("Appearance"),
        "Light": t("Light"),
        "Dark": t("Dark"),
        "Language": t("Language"),
        "Search filters": t("Search filters"),
        "The last used filters are restored when the app starts.": t(
            "The last used filters are restored when the app starts."
        ),
        "Clear saved filters": t("Clear saved filters"),
        "Saved search filters cleared": t("Saved search filters cleared"),
        "Could not clear the saved search filters": t(
            "Could not clear the saved search filters"
        ),
        "Application": t("Application"),
        "Version": t("Version"),
        "Config file": t("Config file"),
        "Keyboard shortcuts": t("Keyboard shortcuts"),
        "Ctrl+F search, Ctrl+D download selected": t(
            "Ctrl+F search, Ctrl+D download selected"
        ),
        "Settings saved": t("Settings saved"),
        "Some settings were not saved": t("Some settings were not saved"),
        "Could not save the settings": t("Could not save the settings"),
        "Could not write the configuration file: {error}": t(
            "Could not write the configuration file: {error}"
        ),
        "The output folder could not be created: {error}": t(
            "The output folder could not be created: {error}"
        ),
        "The output folder isn't writable. Open Settings to choose another folder.": t(
            "The output folder isn't writable. Open Settings to choose another folder."
        ),
        "Enter a folder path": t("Enter a folder path"),
        "That path is a file, not a folder": t("That path is a file, not a folder"),
        "A proxy address cannot contain spaces": t(
            "A proxy address cannot contain spaces"
        ),
        "Use a proxy scheme such as http:// or socks5://": t(
            "Use a proxy scheme such as http:// or socks5://"
        ),
        "Put the proxy username and password in the fields below, "
        "not in the address": t(
            "Put the proxy username and password in the fields below, "
            "not in the address"
        ),
        "Include a host and port, e.g. http://127.0.0.1:7890": t(
            "Include a host and port, e.g. http://127.0.0.1:7890"
        ),
        "Include a port, e.g. http://127.0.0.1:7890": t(
            "Include a port, e.g. http://127.0.0.1:7890"
        ),
        "The proxy port must be a number between 1 and 65535": t(
            "The proxy port must be a number between 1 and 65535"
        ),
        "Unknown language": t("Unknown language"),
        "Unknown theme": t("Unknown theme"),
        "Expected on/off": t("Expected on/off"),
        "Expected a settings object": t("Expected a settings object"),
        "Invalid JSON in {path}": t("Invalid JSON in {path}"),
        "Invalid TOML in {path}": t("Invalid TOML in {path}"),
        "Could not update {key} in {path}": t("Could not update {key} in {path}"),
        # shortcuts
        "Create desktop shortcut": t("Create desktop shortcut"),
        "Shortcuts created on the desktop and Start menu": t(
            "Shortcuts created on the desktop and Start menu"
        ),
        "Shortcut already exists": t("Shortcut already exists"),
        "Shortcuts require the packaged app": t("Shortcuts require the packaged app"),
        "Shortcuts are only supported on Windows": t(
            "Shortcuts are only supported on Windows"
        ),
        "Click here to create a desktop shortcut": t(
            "Click here to create a desktop shortcut"
        ),
        # updates
        "Update available: v{ver}": t("Update available: v{ver}"),
        "New version {ver} is out — click to download": t(
            "New version {ver} is out — click to download"
        ),
        "Download and install": t("Download and install"),
        "Install and restart": t("Install and restart"),
        "No package for this platform in v{ver}; open the release page": t(
            "No package for this platform in v{ver}; open the release page"
        ),
        "Release notes": t("Release notes"),
        "Later": t("Later"),
        "Downloading…": t("Downloading…"),
        "Installing…": t("Installing…"),
        "Update download failed": t("Update download failed"),
        "Could not start the installer": t("Could not start the installer"),
        # subtitles
        "Search novels across all sites, ranked by quality": t(
            "Search novels across all sites, ranked by quality"
        ),
        "Live queue and progress": t("Live queue and progress"),
        "Search and download across 25 music platforms": t(
            "Search and download across 25 music platforms"
        ),
        "Track new chapters of books you care about": t(
            "Track new chapters of books you care about"
        ),
        # placeholders
        "Enter book title or author…": t("Enter book title or author…"),
        "Site filter, comma separated (blank = all)": t(
            "Site filter, comma separated (blank = all)"
        ),
        "Song / artist / album…": t("Song / artist / album…"),
        "Site (e.g. n23qb)": t("Site (e.g. n23qb)"),
        "Book ID": t("Book ID"),
        "Title (optional)": t("Title (optional)"),
        # task states
        "Queued": t("Queued"),
        "Downloading": t("Downloading"),
        "Processing": t("Processing"),
        "Exporting": t("Exporting"),
        "Completed": t("Completed"),
        "Failed": t("Failed"),
        "Cancelled": t("Cancelled"),
        "Exporting {fmt}": t("Exporting {fmt}"),
        "Downloading {done}/{total}": t("Downloading {done}/{total}"),
        # search page
        "Searching…": t("Searching…"),
        "{n} results in {s}s": t("{n} results in {s}s"),
        "{n} sources": t("{n} sources"),
        "{n} results": t("{n} results"),
        "{n} sources failed": t("{n} sources failed"),
        "{s}s": t("{s}s"),
        "Search cancelled": t("Search cancelled"),
        "Search failed": t("Search failed"),
        "No results": t("No results"),
        "Try another keyword or clear the site filter": t(
            "Try another keyword or clear the site filter"
        ),
        "Filters": t("Filters"),
        "Min words": t("Min words"),
        "Max words": t("Max words"),
        "Updated after": t("Updated after"),
        "Clear filters": t("Clear filters"),
        "e.g. 100000": t("e.g. 100000"),
        "Min words: {n}": t("Min words: {n}"),
        "Max words: {n}": t("Max words: {n}"),
        "Updated after {date}": t("Updated after {date}"),
        "Filters applied: {filters}": t("Filters applied: {filters}"),
        "Min words must be a whole number between 0 and {max}": t(
            "Min words must be a whole number between 0 and {max}"
        ),
        "Max words must be a whole number between 0 and {max}": t(
            "Max words must be a whole number between 0 and {max}"
        ),
        "Min words cannot be greater than max words": t(
            "Min words cannot be greater than max words"
        ),
        "Use a YYYY-MM-DD date": t("Use a YYYY-MM-DD date"),
        "Author": t("Author"),
        "Best match": t("Best match"),
        "Show more": t("Show more"),
        "Open in browser": t("Open in browser"),
        "Queued: {title}": t("Queued: {title}"),
        # download page
        "Active": t("Active"),
        # The third bucket holds completed *and* failed/cancelled tasks, so it
        # cannot be labelled "Completed".
        "Finished": t("Finished"),
        "Nothing in the queue": t("Nothing in the queue"),
        "Search for a book and press Download": t(
            "Search for a book and press Download"
        ),
        "No tasks yet": t("No tasks yet"),
        "This task has already finished": t("This task has already finished"),
        "Could not retry this task": t("Could not retry this task"),
        "Copy error": t("Copy error"),
        "Could not copy the error": t("Could not copy the error"),
        "Could not open {path}": t("Could not open {path}"),
        "Could not open the download folder: {error}": t(
            "Could not open the download folder: {error}"
        ),
        # history page
        "{n} files in {dir}": t("{n} files in {dir}"),
        "No exported books yet": t("No exported books yet"),
        "Files land here when a download finishes": t(
            "Files land here when a download finishes"
        ),
        "Could not load this list": t("Could not load this list"),
        # follow page
        "Could not queue this book": t("Could not queue this book"),
        "Could not remove this book from the follow list": t(
            "Could not remove this book from the follow list"
        ),
        # music page
        "All platforms": t("All platforms"),
        "Some platforms need extra setup": t("Some platforms need extra setup"),
        "Go to settings": t("Go to settings"),
        "Searches every platform at once and merges the results": t(
            "Searches every platform at once and merges the results"
        ),
        "The search results expired — search again before downloading": t(
            "The search results expired — search again before downloading"
        ),
        "Search a song to begin": t("Search a song to begin"),
        "Open music folder": t("Open music folder"),
        "Could not open the music folder: {error}": t(
            "Could not open the music folder: {error}"
        ),
        "The system file manager could not open it": t(
            "The system file manager could not open it"
        ),
        "Unknown error": t("Unknown error"),
        # progressive aggregate search + download failure panel
        "Received results from {n} platforms…": t(
            "Received results from {n} platforms…"
        ),
        "{n} platforms did not respond": t("{n} platforms did not respond"),
        "{n} tracks failed": t("{n} tracks failed"),
        "Retry failed": t("Retry failed"),
        "A download is already running": t("A download is already running"),
        "Music extras are not installed": t("Music extras are not installed"),
        "Downloaded {n} tracks": t("Downloaded {n} tracks"),
        "{n} with lyrics": t("{n} with lyrics"),
        "Lyrics": t("Lyrics"),
        "Original lyrics": t("Original lyrics"),
        "Translated lyrics": t("Translated lyrics"),
        "Lyrics for {name}": t("Lyrics for {name}"),
        "Done: {ok} downloaded, {skip} skipped, {fail} failed": t(
            "Done: {ok} downloaded, {skip} skipped, {fail} failed"
        ),
        "Export playlist (.m3u8)": t("Export playlist (.m3u8)"),
        "Playlist saved": t("Playlist saved"),
        "Playlist saved to {path}": t("Playlist saved to {path}"),
        "{n} track(s) without a local file were skipped": t(
            "{n} track(s) without a local file were skipped"
        ),
        "Search songs first; there is nothing to export": t(
            "Search songs first; there is nothing to export"
        ),
        "No track has a local file path yet — download the tracks first": t(
            "No track has a local file path yet — download the tracks first"
        ),
        "Could not write the playlist to {path}: {error}": t(
            "Could not write the playlist to {path}: {error}"
        ),
        "The playlist already exists: {path}": t("The playlist already exists: {path}"),
        "Click to overwrite": t("Click to overwrite"),
        "Copy": t("Copy"),
        "Copied": t("Copied"),
        "Could not copy; the path is selected": t(
            "Could not copy; the path is selected"
        ),
        # detail drawer (narrow windows)
        "Close": t("Close"),
        # follow page
        "Added to follow list": t("Added to follow list"),
        "Already followed": t("Already followed"),
        "No chapter recorded yet": t("No chapter recorded yet"),
        "No followed books": t("No followed books"),
        "Follow a search result or add a book manually": t(
            "Follow a search result or add a book manually"
        ),
        "Checking for updates…": t("Checking for updates…"),
        "Checking {done}/{total}…": t("Checking {done}/{total}…"),
        "Checked {n} books · {u} updated": t("Checked {n} books · {u} updated"),
        "Checked {n} books · No new chapters": t("Checked {n} books · No new chapters"),
        "Checked {n} books, {u} updated, {f} failed": t(
            "Checked {n} books, {u} updated, {f} failed"
        ),
        # dialogs
        "Site and book ID are required": t("Site and book ID are required"),
    }


# ---------------------------------------------------------------------------
# serialisation helpers
# ---------------------------------------------------------------------------


def _task_dict(task: object) -> dict[str, Any]:
    """Serialise a :class:`DownloadTask` for the front end."""
    status_value = getattr(task, "status", None)
    status = str(getattr(status_value, "value", status_value) or "")
    site = str(getattr(task, "site", "") or "")
    book_id = str(getattr(task, "book_id", "") or "")
    try:
        exports = [str(p) for p in dict(getattr(task, "exported_paths", {})).values()]
    except Exception:  # noqa: BLE001 - the dict can be mutated while we read it
        exports = []
    try:
        progress = float(task.progress())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - defensive
        progress = 0.0
    try:
        raw_eta = task.eta()  # type: ignore[attr-defined]
        eta = round(float(raw_eta)) if raw_eta else None
    except Exception:  # noqa: BLE001 - defensive
        eta = None
    return {
        "task_id": str(getattr(task, "task_id", "")),
        "title": str(getattr(task, "title", "") or ""),
        "site": site,
        "book_id": book_id,
        "status": status,
        "status_label": status_label(status_value),
        "progress": progress,
        "chapters_done": int(getattr(task, "chapters_done", 0) or 0),
        "chapters_total": int(getattr(task, "chapters_total", 0) or 0),
        "error": str(getattr(task, "error", None) or ""),
        "retry_count": int(getattr(task, "retry_count", 0) or 0),
        "export_stage": str(getattr(task, "export_stage", None) or ""),
        "exports": exports,
        "created_at": getattr(task, "created_at", None),
        "finished_at": getattr(task, "finished_at", None),
        "eta": eta,
        "retryable": status in {"failed", "cancelled"} and bool(site and book_id),
    }


def _result_dict(result: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Serialise one :class:`SearchResult`; the first hit is flagged best."""
    data = dict(result)
    data["best"] = index == 0
    return data


#: Upper bound for the desktop word-count filters.  Kept in step with the REST
#: schema (``MAX_SEARCH_WORDS``) and the CLI so all three fronts reject the
#: same out-of-range values with the same wording.
_MAX_SEARCH_WORDS = 1_000_000_000
#: The only accepted ``updated_after`` spelling.  A browser date input already
#: yields ``YYYY-MM-DD``; anything else is rejected here instead of being
#: reinterpreted by each site's date parser.
_SEARCH_FILTER_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _search_filter_error(field: str, message: str) -> dict[str, Any]:
    """Build the structured rejection the page maps back to one filter input."""
    return {
        "ok": False,
        "error": message,
        "error_code": "invalid_search_filter",
        "error_field": field,
    }


def _word_filter(field: str, value: object) -> tuple[int | None, dict[str, Any] | None]:
    """Validate one optional word-count bound from the JS bridge.

    ``None`` and blank strings mean "no filter".  Numeric strings, ints and
    integral floats are accepted so a bridge that serialises a JS number
    either way keeps working, while booleans, fractions and junk are rejected
    with a payload naming the offending input.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        number: int | None = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        number = int(text) if text.isdigit() else None
    else:
        number = None
    max_words = _MAX_SEARCH_WORDS
    if field == "min_words":
        message = t("Min words must be a whole number between 0 and {max}").format(
            max=max_words
        )
    else:
        message = t("Max words must be a whole number between 0 and {max}").format(
            max=max_words
        )
    if number is None or not 0 <= number <= max_words:
        return None, _search_filter_error(field, message)
    return number, None


def _parse_search_filters(
    min_words: object,
    max_words: object,
    updated_after: object,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Normalise the search filter arguments into keyword arguments.

    Returns ``(kwargs, error)``.  ``error`` is the structured payload the page
    displays and focuses on; ``kwargs`` is empty in that case.  Blank values are
    omitted from ``kwargs`` so the search layer sees exactly the active filters.
    """
    minimum, error = _word_filter("min_words", min_words)
    if error is not None:
        return {}, error
    maximum, error = _word_filter("max_words", max_words)
    if error is not None:
        return {}, error
    if minimum is not None and maximum is not None and minimum > maximum:
        return {}, _search_filter_error(
            "max_words", t("Min words cannot be greater than max words")
        )

    filters: dict[str, Any] = {}
    if minimum is not None:
        filters["min_words"] = minimum
    if maximum is not None:
        filters["max_words"] = maximum
    if updated_after is not None:
        raw_date = str(updated_after).strip()
        if raw_date:
            message = t("Use a YYYY-MM-DD date")
            if not _SEARCH_FILTER_DATE_RE.fullmatch(raw_date):
                return {}, _search_filter_error("updated_after", message)
            try:
                filters["updated_after"] = date.fromisoformat(raw_date)
            except ValueError:
                return {}, _search_filter_error("updated_after", message)
    return filters, None


def _music_track_dict(track: object) -> dict[str, Any]:
    """
    Serialise a music provider track dataclass for the front end.

    ``extra`` is dropped on purpose: it holds provider-private objects (musicdl
    ``SongInfo``, NetEase raw payloads) that JSON cannot carry. Downloading goes
    through :data:`novel_downloader.apps.music.trackcache.CACHE` instead, which
    hands the engine the original objects back.
    """
    try:
        data = dict(dataclasses.asdict(track))  # type: ignore[call-overload]
    except Exception:  # noqa: BLE001 - fall back to attribute copy
        sources = getattr(track, "sources", [])
        return {
            "provider": str(getattr(track, "provider", "") or ""),
            "id": str(getattr(track, "id", "") or ""),
            "name": str(getattr(track, "name", "") or ""),
            "artists": str(getattr(track, "artists", "") or ""),
            "album": str(getattr(track, "album", "") or ""),
            "duration_s": int(getattr(track, "duration_s", 0) or 0),
            "vip": bool(getattr(track, "vip", False)),
            "ext": str(getattr(track, "ext", "") or ""),
            "sources": (
                [str(item) for item in sources] if isinstance(sources, list) else []
            ),
        }
    data.pop("extra", None)
    return data


def _format_size(size: int) -> str:
    """Human-readable byte size (kept aligned with the CLI's formatting)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_time(ts: float) -> str:
    """Local ``YYYY-MM-DD HH:MM`` for a POSIX timestamp."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def _format_health_ms(elapsed_ms: int) -> str:
    """Render a health-check latency."""
    if elapsed_ms >= 1000:
        return f"{elapsed_ms / 1000:.1f}s"
    return f"{elapsed_ms}ms"


# ---------------------------------------------------------------------------
# update bookkeeping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _UpdateCache:
    """One interpreted update check, as served by ``update_state``."""

    info: UpdateInfo | None
    error: str | None
    checked_at: float
    current: str
    platform: str


@dataclass(frozen=True)
class _DownloadedUpdate:
    """A downloaded asset whose checksum was verified by us.

    ``version`` is the release the file was fetched for.  It stays optional so
    records restored from older builds (and tests that only care about the
    file) remain valid; a record without a version is never treated as stale.
    """

    path: Path
    sha256: str
    name: str
    version: str | None = None


def _idle_download_state() -> dict[str, Any]:
    """The "nothing in flight" download payload, carrying every public key."""
    return {
        "active": False,
        "ok": False,
        "received": 0,
        "total": 0,
        "error": None,
        "path": None,
        "version": None,
    }


def _normalise_version(value: str | None) -> str:
    """Compare versions without the cosmetic ``v`` prefix or whitespace."""
    return str(value or "").strip().lstrip("vV").strip()


#: Handle of the process-lifetime update mutex (Windows only).  Kept at module
#: level so the handle outlives the call; the kernel releases it when the
#: process exits.
_MUTEX_HANDLE: int | None = None


def _hold_update_mutex() -> None:
    """
    Create the named mutex named by the installer's ``AppMutex`` setting.

    Held for the lifetime of the process, it lets Inno Setup's Restart Manager
    find this instance deterministically before replacing the binaries.

    Windows only and strictly best effort: a missing ``ctypes``/kernel API, or
    any failure at all, simply skips the mutex instead of breaking startup.
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32" or _MUTEX_HANDLE is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Declare the prototype instead of relying on ctypes' defaults: the
        # return value is a 64-bit HANDLE (a default ``c_int`` would truncate
        # it) and the mutex name is a wide string (a default narrow ``c_char_p``
        # conversion would pass the wrong pointer).
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        handle = int(kernel32.CreateMutexW(None, False, _UPDATE_MUTEX_NAME) or 0)
    except Exception:  # noqa: BLE001 - the mutex is an optimisation, not a need
        logger.debug("Could not create the update mutex", exc_info=True)
        return
    if handle:
        _MUTEX_HANDLE = handle


def _updates_dir() -> Path:
    """
    Directory receiving downloaded updates (``app_home()/updates``).

    Created on demand so :func:`download_asset` always has a root to stay
    inside.

    :return: The resolved update directory.
    """
    from novel_downloader.infra.paths import app_home

    path = Path(app_home()) / _UPDATES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _portable_keep_paths(app_dir: Path) -> tuple[str, ...]:
    """
    User-data paths the portable helper must not delete.

    Updates clear ``<app_dir>/_internal`` (the PyInstaller payload).  User
    settings and downloads normally live in :func:`app_home` outside the
    program folder and are never touched.  The only overlap is an explicit
    ``NOVEL_HOME`` pointing inside the payload; that subtree is handed to the
    helper so it is preserved instead of being removed with the old build.

    :param app_dir: directory containing the running executable.
    :return: absolute user-data paths inside ``_internal``, possibly empty.
    """
    try:
        payload = (app_dir / _PAYLOAD_DIRNAME).resolve()
    except OSError:
        return ()
    try:
        from novel_downloader.infra.paths import app_home

        home = Path(app_home()).resolve()
    except (OSError, RuntimeError):
        return ()
    try:
        if home == payload or home.is_relative_to(payload):
            return (str(home),)
    except ValueError:
        return ()
    return ()


def _prune_updates(updates_dir: Path) -> None:
    """
    Delete stale update artefacts so ``updates/`` cannot grow forever.

    Interrupted ``.part`` files live for a day (a retry may still use them),
    finished installers/zips for a week.  Nothing here is fatal: a file that
    cannot be removed (still mapped by a running installer) is simply left for
    the next pass.
    """
    now = time.time()
    try:
        entries = list(updates_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            stat = entry.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        limit = _PRUNE_PART_AGE if entry.suffix.lower() == ".part" else _PRUNE_FILE_AGE
        if age <= limit:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            logger.debug("Could not prune %s", entry, exc_info=True)


def _inside_dir(path: Path, root: Path) -> bool:
    """Whether ``path`` resolves inside ``root`` (``False`` on I/O errors)."""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def _is_https_asset_url(url: str) -> bool:
    """
    Whether an asset URL uses the only scheme the desktop updater fetches.

    Unlike the CLI's :func:`novel_downloader.apps.common.updater.download_asset`
    (which accepts plain HTTP so local mirrors keep working), the GUI download
    path is restricted to HTTPS: the file it writes is later *executed*, so a
    plaintext transfer that could be rewritten in flight is not acceptable.
    """
    return str(url or "").strip().lower().startswith("https://")


def _prefs_flag(prefs: Mapping[str, Any], key: str, default: bool) -> bool:
    """Read a boolean preference, tolerating strings written by hand."""
    value = prefs.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    if value is None:
        return default
    return bool(value)


def _update_due(prefs: Mapping[str, Any], now: float) -> bool:
    """Whether the 24-hour throttle allows a new check."""
    try:
        last = float(prefs.get("last_update_check") or 0.0)
    except (TypeError, ValueError):
        # A hand-edited preference file must not break the check.
        last = 0.0
    return (now - last) >= _UPDATE_CHECK_INTERVAL


def _safe_platform_tag() -> str:
    """``detect_platform()`` that degrades to ``""`` instead of raising."""
    try:
        tag: str = detect_platform()
        return tag
    except Exception:  # noqa: BLE001 - a probe failure is not fatal
        logger.debug("Could not detect the platform tag", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------


def _prefs_path() -> Path:
    """Location of the desktop preference file."""
    from novel_downloader.apps.music.paths import music_data_dir

    return music_data_dir() / _PREFS_FILENAME


def _load_prefs() -> dict[str, Any]:
    """Read stored desktop preferences (best effort)."""
    try:
        loaded = json.loads(_prefs_path().read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


#: How long a read-modify-write may wait for another process's lock before
#: the caller reports a structured failure.  Bridge saves run on the UI
#: thread, so a wedged peer must never freeze the window indefinitely.
_LOCK_TIMEOUT = 2.0
#: Polling interval while a peer holds the file lock.
_LOCK_POLL_INTERVAL = 0.05

#: ``errno`` values a contended lock reports (POSIX ``flock`` and Windows
#: ``msvcrt.locking`` disagree on the code, so all of them are accepted).
_LOCK_CONTENTION_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EWOULDBLOCK", None),
    )
    if code is not None
)

#: In-process locks, one per target file.  ``flock``/``msvcrt`` protect the
#: data file across processes; this keeps threads of one process serialised
#: as well (Windows byte-range locks are per process) and is the fallback
#: when the platform cannot lock files at all.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class _LockTimeoutError(TimeoutError):
    """A settings/prefs lock could not be acquired before the deadline."""


def _lock_path(target: Path) -> Path:
    """Return the sidecar lock file: ``<target>.lock`` in the same folder."""
    return target.with_name(target.name + ".lock")


def _thread_lock_for(target: Path) -> threading.RLock:
    """Return the process-local lock shared by every caller for ``target``."""
    key = os.path.normcase(os.path.abspath(os.fspath(target)))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _open_lock_file(path: Path) -> int:
    """
    Open (creating) the sidecar lock file without following a symlink.

    A symlink planted where the lock file belongs is removed (the link
    itself, not its target) and replaced with a regular file; ``O_NOFOLLOW``
    closes the check/open race on POSIX.  The file is private to the user
    (0600) on POSIX even when a pre-existing file had looser bits.
    """
    if path.is_symlink():
        with contextlib.suppress(OSError):
            path.unlink()
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    if os.name != "posix" and path.is_symlink():
        # Windows has no ``O_NOFOLLOW``: re-check after opening so a link
        # swapped in between the pre-check and the open is never used.
        os.close(fd)
        raise OSError(errno.ELOOP, "lock path is a symlink")
    if os.name == "posix":
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            with contextlib.suppress(OSError):
                fchmod(fd, 0o600)
    return fd


def _lock_fd(fd: int) -> None:
    """Take an exclusive non-blocking lock on ``fd``'s first byte."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    # The POSIX-only members are absent from the Windows type stubs mypy uses;
    # this branch never runs there, so the ignores are safe.
    fcntl.flock(  # type: ignore[attr-defined]
        fd,
        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
    )


def _unlock_fd(fd: int) -> None:
    """Release :func:`_lock_fd` (best effort; closing ``fd`` also drops it)."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _acquire_file_lock(fd: int, deadline: float) -> None:
    """
    Retry :func:`_lock_fd` until ``deadline``.

    :raises _LockTimeoutError: when another process still holds the lock.
    :raises OSError: when the platform cannot lock files (the caller then
        degrades to the in-process lock).
    """
    while True:
        try:
            _lock_fd(fd)
            return
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _LockTimeoutError(t("already in use")) from exc
            time.sleep(min(_LOCK_POLL_INTERVAL, remaining))


@contextlib.contextmanager
def _cross_process_lock(
    target: Path, *, timeout: float | None = None
) -> Iterator[None]:
    """
    Serialise a read-modify-write on ``target`` across threads and processes.

    The lock is an advisory exclusive lock on ``<target>.lock`` in the same
    directory as ``target``.  When file locking is unavailable (unsupported
    filesystem, unusable lock path), the in-process lock is used alone and a
    debug message records the degradation.  Waiting is bounded by ``timeout``
    so a bridge call can never block the UI thread forever.

    :raises _LockTimeoutError: when the lock is still held at the deadline.
    """
    budget = _LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + max(0.0, budget)
    thread_lock = _thread_lock_for(target)
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise _LockTimeoutError(t("already in use"))

    fd: int | None = None
    try:
        lock_path = _lock_path(target)
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = _open_lock_file(lock_path)
        except OSError as exc:
            logger.debug(
                "Cross-process lock unavailable for %s (%s); using the in-process lock",
                target,
                exc,
            )
        if fd is not None:
            try:
                _acquire_file_lock(fd, deadline)
            except _LockTimeoutError:
                raise
            except OSError as exc:
                logger.debug(
                    "Cross-process lock unavailable for %s (%s); "
                    "using the in-process lock",
                    target,
                    exc,
                )
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                _unlock_fd(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        thread_lock.release()


#: Boolean switches the front end is allowed to write through ``save_prefs``.
_PREF_BOOL_KEYS: tuple[str, ...] = (
    "auto_check_updates",
    "auto_download_updates",
    "auto_install_on_exit",
)

#: Schemes the shared HTTP stack understands for ``general.proxy``.
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})


def _write_text_atomic(path: Path, text: str) -> None:
    """
    Replace ``path`` with ``text`` without exposing a half-written file.

    A truncate-in-place write can be observed by a concurrent reader (or by
    the next launch after a crash) as an empty preference/config file.  The
    temporary file lives in the same directory so ``os.replace`` stays atomic
    on every supported filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _prefs_file_is_corrupt(path: Path) -> bool:
    """Whether ``path`` exists but does not hold a JSON object."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Unreadable is an I/O problem; a backup copy would not help.
        return False
    except ValueError:
        # Undecodable bytes count as corruption worth preserving.
        return True
    if not text.strip():
        return False
    try:
        return not isinstance(json.loads(text), dict)
    except ValueError:
        return True


def _preserve_corrupt_prefs(path: Path) -> bool:
    """Keep the bytes of an unreadable preference file next to it."""
    backup = path.with_name(path.name + ".broken")
    try:
        os.replace(path, backup)
    except OSError:
        logger.warning("Could not preserve the corrupt prefs file %s", path)
        return False
    logger.warning("Moved an unreadable prefs file to %s", backup)
    return True


def _save_prefs(values: dict[str, Any]) -> bool:
    """
    Merge ``values`` into the preference file (best effort).

    A file that cannot be parsed is moved aside as ``<name>.broken`` before
    the merged content is written, so a crash or a hand edit never destroys
    data silently.

    :return: ``True`` when the file was written.  Callers that show feedback
        (the settings page) must not tell the user their change was saved
        when the profile directory is read-only.
    """
    path = _prefs_path()
    try:
        with _cross_process_lock(path):
            stored = _load_prefs()
            if _prefs_file_is_corrupt(path):
                _preserve_corrupt_prefs(path)
            stored.update(values)
            _write_text_atomic(path, json.dumps(stored, ensure_ascii=False, indent=2))
    except _LockTimeoutError:
        # Another instance held the lock for the whole budget.  The file is
        # left untouched, so the caller can report a retryable failure.
        logger.warning("Timed out waiting for the desktop prefs lock: %s", path)
        return False
    except (OSError, TypeError, ValueError):
        # ``TypeError``/``ValueError`` guard against unserialisable stored
        # content; preferences are best effort and never fatal.
        logger.debug("Could not persist desktop prefs", exc_info=True)
        return False
    return True


def _remove_prefs_keys(*keys: str) -> bool:
    """
    Delete ``keys`` from the preference file (best effort).

    The read-modify-write follows the same contract as :func:`_save_prefs`:
    serialised across processes, atomic, and a corrupt file is preserved as
    ``<name>.broken`` instead of being discarded.  A missing file (or missing
    keys) is a success, so clearing twice never fails.

    :return: ``True`` when the file no longer holds the keys.
    """
    path = _prefs_path()
    try:
        with _cross_process_lock(path):
            stored = _load_prefs()
            if _prefs_file_is_corrupt(path):
                _preserve_corrupt_prefs(path)
            if not any(key in stored for key in keys):
                return True
            for key in keys:
                stored.pop(key, None)
            _write_text_atomic(path, json.dumps(stored, ensure_ascii=False, indent=2))
    except _LockTimeoutError:
        logger.warning("Timed out waiting for the desktop prefs lock: %s", path)
        return False
    except (OSError, TypeError, ValueError):
        logger.debug("Could not persist desktop prefs", exc_info=True)
        return False
    return True


def _coerce_bool(value: Any) -> bool | None:
    """Strict-ish boolean coercion for values arriving over the JS bridge."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return None


def _prefs_theme(prefs: Mapping[str, Any]) -> str:
    """Only ``light``/``dark`` are valid; anything else falls back to light."""
    theme = str(prefs.get("theme") or "light")
    return theme if theme in ("light", "dark") else "light"


def _stored_search_filters(prefs: Mapping[str, Any]) -> dict[str, Any]:
    """
    Return the saved search filters, dropping invalid stored values.

    The preference file can be hand-edited or written by an older build, so
    each field is validated in isolation:

    * an invalid ``min_words``/``max_words`` is ignored on its own;
    * a pair where the minimum exceeds the maximum is discarded as a whole
      (the combination was never a valid search);
    * an invalid or non-``YYYY-MM-DD`` date is ignored.

    The result always uses JSON-friendly types (ints and an ISO date string)
    so it can be injected into the page unchanged.
    """
    value = prefs.get("search_filters")
    if not isinstance(value, Mapping):
        return {}
    minimum, _ = _word_filter("min_words", value.get("min_words"))
    maximum, _ = _word_filter("max_words", value.get("max_words"))
    if minimum is not None and maximum is not None and minimum > maximum:
        minimum = None
        maximum = None
    stored: dict[str, Any] = {}
    if minimum is not None:
        stored["min_words"] = minimum
    if maximum is not None:
        stored["max_words"] = maximum
    raw_date = value.get("updated_after")
    if raw_date is not None:
        text = str(raw_date).strip()
        if text and _SEARCH_FILTER_DATE_RE.fullmatch(text):
            with contextlib.suppress(ValueError):
                stored["updated_after"] = date.fromisoformat(text).isoformat()
    return stored


# ---------------------------------------------------------------------------
# output directory / configuration file
# ---------------------------------------------------------------------------


def _configured_value(key: str) -> str:
    """Read a string from ``[general]`` (``""`` when absent or not a string)."""
    try:
        from novel_downloader.infra.config import get_config_value

        value = get_config_value(["general", key], "")
    except Exception:  # noqa: BLE001 - config may be missing on first run
        return ""
    return value.strip() if isinstance(value, str) else ""


def _configured_output_dir() -> str:
    """The raw ``general.output_dir`` value (``./downloads`` by default)."""
    text = _configured_value("output_dir")
    return text or "./downloads"


def _expand_output_dir(raw: str) -> Path:
    """
    Expand ``~``/environment variables and anchor relative paths.

    The desktop process chdirs to the application home, so a bare
    ``downloads`` keeps the historical meaning there; expanding it up front
    is what stops the settings page from saving an unexpanded ``~/books``
    that the downloader would later resolve differently.
    """
    text = str(raw).strip()
    try:
        path = Path(os.path.expandvars(text)).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    except (OSError, ValueError, RuntimeError):
        # An unresolvable ``~``/variable leaves us with the literal input; the
        # probe below turns that into a structured error instead of a crash.
        return Path(text or ".")
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        # ``resolve`` stats the path, so a NUL byte or a symlink loop raises
        # here.  Returning the anchored path keeps every caller total;
        # :func:`_probe_directory` reports the real problem when saving.
        return path


def _directory_status(path: Path) -> dict[str, Any]:
    """
    Describe a directory without creating it.

    ``state`` is one of ``ready``, ``missing``, ``not_writable``,
    ``not_a_dir`` or ``error``.  The settings page renders the matching hint
    and the first-run boot uses it to warn instead of failing a download
    later.
    """
    state = "ready"
    error: str | None = None
    try:
        # ``Path.exists`` swallows the ValueError a NUL byte raises and would
        # report "missing", which invites the page to create an impossible
        # path.  Surface it as the unusable configuration it is.
        if "\x00" in str(path):
            raise ValueError("embedded null byte in path")
        if not path.exists():
            state = "missing"
        elif not path.is_dir():
            state = "not_a_dir"
        elif not os.access(path, os.W_OK):
            state = "not_writable"
    except (OSError, ValueError) as exc:
        state = "error"
        error = format_error(exc)
    return {"path": str(path), "state": state, "error": error}


def _probe_directory(path: Path) -> str | None:
    """
    Create ``path`` if needed and prove it accepts a file.

    :return: A human-readable error, or ``None`` when the directory is usable.
    """
    try:
        if path.exists() and not path.is_dir():
            return t("That path is a file, not a folder")
        path.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".noveldl-write-", dir=str(path))
        try:
            os.close(handle)
        finally:
            # The directory accepted a file, which is all we needed to prove:
            # a lock on the temp file must not turn into "not writable".
            with contextlib.suppress(OSError):
                Path(temp_name).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        return format_error(exc)
    return None


def _settings_config_path() -> Path:
    """The config file to edit, mirroring ``load_config``'s lookup order."""
    from novel_downloader.infra.paths import app_home

    home = app_home()
    for base in (Path.cwd(), home):
        for name in ("settings.toml", "settings.json"):
            candidate = base / name
            if candidate.is_file():
                return candidate
    return home / "settings.toml"


_TOML_HEADER_RE = re.compile(r"^[ \t]*\[([^\[\]]+)\][ \t]*(?:#.*)?\r?\n?$")
_TOML_KEY_RE = re.compile(r"^[ \t]*(?:[A-Za-z0-9_.-]+|\"[^\"]+\"|'[^']+')\s*=")


def _split_toml_comment(line: str) -> tuple[str, str]:
    """Split ``key = value  # note`` without tripping over ``#`` in a string."""
    in_basic = False
    in_literal = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_basic:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_basic = False
        elif in_literal:
            if char == "'":
                in_literal = False
        elif char == '"':
            in_basic = True
        elif char == "'":
            in_literal = True
        elif char == "#":
            return line[:index], line[index:]
    return line, ""


def _toml_value(value: Any) -> str:
    """Render a scalar as a TOML literal (JSON string escapes are compatible)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _patch_toml_text(text: str, section: str, values: Mapping[str, Any]) -> str:
    """
    Set ``section``'s keys in ``text`` while preserving comments and layout.

    Only the touched lines change: rewriting the whole document through a
    serialiser would drop every explanatory comment in the shipped sample.
    """
    lines = text.splitlines(keepends=True)
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _TOML_HEADER_RE.match(line)
        if match is None:
            continue
        name = match.group(1).strip()
        if start is None:
            if name == section:
                start = index
        elif name != section:
            end = index
            break

    if start is None:
        if text and not text.endswith(("\n", "\r")):
            text += "\n"
        block = [f"[{section}]\n"]
        block.extend(f"{key} = {_toml_value(value)}\n" for key, value in values.items())
        separator = "\n" if text and not text.endswith("\n\n") else ""
        return text + separator + "".join(block)

    for key, value in values.items():
        pattern = re.compile(
            rf"^[ \t]*(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*="
        )
        found: int | None = None
        for index in range(start + 1, end):
            if pattern.match(lines[index]):
                found = index
                break
        if found is not None:
            line = lines[found]
            ending = ""
            for candidate in ("\r\n", "\n"):
                if line.endswith(candidate):
                    ending = candidate
                    break
            body = line[: len(line) - len(ending)] if ending else line
            code, comment = _split_toml_comment(body)
            indent = code[: len(code) - len(code.lstrip())]
            suffix = f"  {comment}" if comment else ""
            lines[found] = f"{indent}{key} = {_toml_value(value)}{suffix}{ending}"
        else:
            insert_at = start + 1
            for index in range(start + 1, end):
                if _TOML_KEY_RE.match(lines[index]):
                    insert_at = index + 1
            if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
                # A hand-edited file can end at ``[general]`` without a
                # newline; inserting there would glue the key to the header.
                lines.insert(insert_at, "\n")
                insert_at += 1
                end += 1
            lines.insert(insert_at, f"{key} = {_toml_value(value)}\n")
            end += 1
    return "".join(lines)


def _update_config_section(section: str, values: Mapping[str, Any]) -> None:
    """
    Persist ``values`` under ``[section]`` in the active config file.

    Raises :class:`OSError`/:class:`ValueError` when the file cannot be read,
    parsed or written; the caller reports that instead of claiming success.
    """
    if not values:
        return
    # Read-modify-write must not interleave with another bridge save (in this
    # or another process), or the later write would silently drop the first
    # one's keys.
    path = _settings_config_path()
    with _cross_process_lock(path):
        _apply_config_section(section, values, path)


def _apply_config_section(
    section: str, values: Mapping[str, Any], path: Path | None = None
) -> None:
    """Implementation of :func:`_update_config_section` (lock held)."""
    if path is None:
        path = _settings_config_path()
    if path.suffix.lower() == ".json":
        data: dict[str, Any] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(t("Invalid JSON in {path}").format(path=path))
            data = loaded
        node = data.get(section)
        if not isinstance(node, dict):
            node = {}
            data[section] = node
        node.update(dict(values))
        _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
        return

    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE

        text = DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")
    updated = _patch_toml_text(text, section, values)

    import tomllib

    parsed = tomllib.loads(updated)
    node = parsed.get(section)
    if not isinstance(node, dict):
        raise ValueError(t("Invalid TOML in {path}").format(path=path))
    for key, value in values.items():
        if node.get(key) != value:
            raise ValueError(
                t("Could not update {key} in {path}").format(key=key, path=path)
            )
    _write_text_atomic(path, updated)


def _split_proxy_credentials(value: Any) -> tuple[str, str, str]:
    """
    Split ``scheme://user:pass@host:port`` into an address and the secrets.

    ``general.proxy`` is an address; credentials belong in ``proxy_user`` /
    ``proxy_pass`` because that is what the HTTP stack reads.  Older files may
    still carry them inline, and stripping the userinfo here is what stops
    :meth:`ApiBridge.get_settings` from echoing a password back into the page.

    :return: ``(address, username, password)``; the last two are ``""`` when
        the value has no userinfo.
    """
    raw = str(value or "").strip()
    if not raw or "@" not in raw:
        return raw, "", ""
    candidate = raw if "://" in raw else "http://" + raw
    try:
        parts = urlsplit(candidate)
    except ValueError:
        # A URL the parser rejects (unclosed IPv6 bracket, ...) can still
        # carry ``user:pass@``; drop that textually so a malformed value
        # never echoes a secret to the page.
        head, separator, _rest = raw.partition("://")
        _userinfo, _at, hostpart = raw.rpartition("@")
        if not hostpart:
            return raw, "", ""
        return (head + separator if separator else "") + hostpart, "", ""
    if "@" not in parts.netloc:
        return raw, "", ""
    hostport = parts.netloc.rsplit("@", 1)[1]
    address = urlunsplit(
        (parts.scheme, hostport, parts.path, parts.query, parts.fragment)
    )
    return address, parts.username or "", parts.password or ""


def _normalize_proxy(value: Any) -> tuple[str, str | None]:
    """
    Validate a proxy address and add ``http://`` when the scheme is omitted.

    Credentials embedded in the URL are rejected: they would be stored and
    echoed as part of ``general.proxy``, and the downloader only sends
    ``proxy_user``/``proxy_pass`` to the HTTP stack.

    :return: ``(normalised_address, error)``; ``error`` is ``None`` on success
        and empty input means "no proxy".
    """
    raw = str(value or "").strip()
    if not raw:
        return "", None
    if any(char.isspace() for char in raw):
        return raw, t("A proxy address cannot contain spaces")
    candidate = raw if "://" in raw else "http://" + raw
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return raw, t("The proxy port must be a number between 1 and 65535")
    if parts.scheme.lower() not in _PROXY_SCHEMES:
        return raw, t("Use a proxy scheme such as http:// or socks5://")
    if parts.username is not None or parts.password is not None:
        return raw, t(
            "Put the proxy username and password in the fields below, "
            "not in the address"
        )
    if not parts.hostname:
        return raw, t("Include a host and port, e.g. http://127.0.0.1:7890")
    if port is None:
        return raw, t("Include a port, e.g. http://127.0.0.1:7890")
    if port < 1:
        # ``urlsplit`` already rejects negative/non-numeric/out-of-range
        # values; zero is the one it still lets through, and no proxy listens
        # there (the HTTP stack would treat it as "default port").
        return raw, t("The proxy port must be a number between 1 and 65535")
    return candidate, None


def _active_language() -> str:
    """The language ``t()`` is currently using (never raises)."""
    try:
        from novel_downloader.infra.i18n import get_language

        return get_language()
    except Exception:  # noqa: BLE001 - a broken catalog must not hide the UI
        logger.debug("Could not resolve the active language", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


class ApiBridge:
    """
    The object exposed to JavaScript as ``window.pywebview.api``.

    Public methods are invoked from pywebview worker threads; they must never
    touch UI state and must return JSON-serialisable values.
    """

    def __init__(self) -> None:
        # ``engine`` imports ``asyncio`` (and is only needed once something
        # actually runs).  Creating it on first use keeps the window path free
        # of that import; the queue restore or a search creates it later.
        self._engine_instance: AsyncEngine | None = None
        self._lock = threading.Lock()
        # music download state (polled by the front end)
        self._music_active = False
        self._music_done = 0
        self._music_total = 0
        self._music_fraction = 0.0
        self._music_stop = threading.Event()
        self._music_summary = ""
        self._music_failed = False
        # Local paths of the submitted batch, aligned by index; the front end
        # merges them back onto its search rows for playlist export.
        self._music_paths: list[str] = []
        # Per-track result payloads, aligned with ``_music_paths`` and the
        # submitted batch. ``path``, ``lyric_path`` and ``lyric_paths`` are
        # only present for files that exist, so the page never renders a dead
        # open button.
        self._music_results: list[dict[str, Any]] = []
        # Failed rows of the last download batch, ordered by track index; the
        # front end renders them with a "retry failed" button.
        self._music_failed_items: list[dict[str, Any]] = []
        # health check state
        self._health_rows: dict[str, dict[str, Any]] = {}
        self._health_thread: threading.Thread | None = None
        # update check cache / download bookkeeping
        self._update_guard = threading.Lock()
        self._update_cache: _UpdateCache | None = None
        self._update_thread: threading.Thread | None = None
        self._download_guard = threading.Lock()
        self._download_active = False
        self._downloaded: _DownloadedUpdate | None = None
        # Byte-level transfer progress, polled by the update bar.  The auto
        # download runs in the background, so without this the page can only
        # show "Downloading…" for however long a 60 MB asset takes.
        self._update_download_state: dict[str, Any] = _idle_download_state()
        self._exit_callback: Callable[[], None] | None = None
        # follow-list check: the sweep blocks one bridge thread for a while,
        # so progress and per-book outcomes are published here for the page to
        # poll while ``check_updates`` is still running.
        self._follow_guard = threading.Lock()
        self._follow_check: dict[str, Any] = {
            "running": False,
            "total": 0,
            "done": 0,
            "updated": 0,
            "failed": 0,
            "error": None,
            "failures": [],
            "updates": [],
        }
        # progressive search state, keyed by search id (polled by the page)
        self._searches: dict[str, dict[str, Any]] = {}
        # progressive music searches, keyed by their own search id
        self._music_searches: dict[str, dict[str, Any]] = {}
        # Background task-queue restore, so window creation never waits for it.
        self._services_thread: threading.Thread | None = None

    @property
    def _engine(self) -> AsyncEngine:
        """The background asyncio engine, started on first use."""
        engine = self._engine_instance
        if engine is None:
            from novel_downloader.apps.desktop.engine import AsyncEngine

            engine = AsyncEngine()
            self._engine_instance = engine
        return engine

    def start_services(self) -> None:
        """
        Restore the persisted download queue for this app session.

        Called once from :func:`run_webview_app` (never from ``__init__``, so
        constructing a bridge in tests cannot touch a real queue file).
        """

        async def run() -> None:
            from novel_downloader.apps.core.task_manager import manager

            await manager.start()

        try:
            self._engine.submit(run()).result(TASK_WAIT_TIMEOUT)
        except Exception:  # noqa: BLE001 - the app still works without a queue
            logger.exception("Could not restore the task queue")

    def start_services_background(self) -> threading.Thread:
        """
        Restore the queue on a daemon thread and return immediately.

        The queue restore imports the task manager (and ``aiohttp``) and reads
        ``tasks.json``.  Neither is needed to show the window, so it runs
        after :class:`ApiBridge` construction while pywebview paints.  The
        restore is idempotent; an early ``add_task`` still calls
        :meth:`~TaskManager.start` itself.
        """
        thread = threading.Thread(
            target=self.start_services,
            name="novel-desktop-services",
            daemon=True,
        )
        self._services_thread = thread
        thread.start()
        return thread

    # ------------------------------------------------------------------
    # system
    # ------------------------------------------------------------------

    def get_initial_state(self) -> dict[str, Any]:
        """One-shot bootstrap payload for the front end."""
        prefs = _load_prefs()
        data_dir = self.get_data_dir()
        theme = _prefs_theme(prefs)
        try:
            from novel_downloader.infra.i18n import SUPPORTED_LANGUAGES

            languages = dict(SUPPORTED_LANGUAGES)
        except Exception:  # noqa: BLE001 - catalog problems are not fatal here
            logger.debug("Could not list the supported languages", exc_info=True)
            languages = {}
        return {
            "version": self._version(),
            "theme": theme,
            # Whether ``theme`` came from disk or is just the default.  Without
            # this the page would overwrite the per-window ``localStorage``
            # choice with "light" when the profile file was unwritable.
            "theme_stored": str(prefs.get("theme") or "") in ("light", "dark"),
            "data_dir": data_dir,
            # First-run check: the boot script warns when the folder exists but
            # cannot be written, instead of letting the first download fail.
            "output_dir": _directory_status(Path(data_dir)),
            "is_frozen": bool(getattr(sys, "frozen", False)),
            "shortcut_exists": self.shortcut_exists(),
            "auto_check_updates": _prefs_flag(prefs, "auto_check_updates", True),
            "auto_download_updates": _prefs_flag(prefs, "auto_download_updates", True),
            "auto_install_on_exit": _prefs_flag(prefs, "auto_install_on_exit", True),
            # The page fills the filter panel from this and does not start a
            # search on its own; invalid/old values are already filtered out.
            "search_filters": _stored_search_filters(prefs),
            "language": _active_language(),
            "languages": languages,
            "sites": {
                "search": dict(SEARCH_SUPPORT_SITES),
                "download": dict(DOWNLOAD_SUPPORT_SITES),
            },
            "i18n": _i18n_catalog(),
        }

    @staticmethod
    def _version() -> str:
        try:
            from novel_downloader import __version__
        except ImportError:  # pragma: no cover - packaging fallback
            return ""
        return str(__version__)

    def get_data_dir(self) -> str:
        """
        Directory the history page lists exported books from.

        This is the resolved ``general.output_dir`` (``./downloads`` by
        default) — the same root :meth:`get_history` scans and
        :meth:`open_downloads_dir` opens.  It used to answer the process
        working directory, so the page's "n files in <dir>" sub-line named the
        application home instead of the folder the files actually sit in.
        """
        try:
            return str(_expand_output_dir(_configured_output_dir()))
        except OSError:  # pragma: no cover - exotic filesystem failures
            return _configured_output_dir()

    def save_prefs(self, values: dict[str, Any]) -> dict[str, Any]:
        """
        Persist a validated subset of the front-end preferences.

        Only the theme and the three update switches are accepted: the file is
        also read by the updater, so arbitrary keys would be an unnecessary
        injection surface.  Unknown keys are ignored rather than stored.

        Never raises.

        :return: ``{"ok", "error", "ignored"}``; ``ok`` is ``False`` when the
            profile directory refused the write (the page then tells the user
            instead of silently reverting on the next launch).
        """
        if not isinstance(values, dict):
            return {
                "ok": False,
                "error": t("Expected on/off"),
                "ignored": [],
            }
        accepted: dict[str, Any] = {}
        errors: dict[str, str] = {}
        if "theme" in values:
            theme = str(values.get("theme") or "").strip().lower()
            if theme in ("light", "dark"):
                accepted["theme"] = theme
            else:
                errors["theme"] = t("Unknown theme")
        for key in _PREF_BOOL_KEYS:
            if key not in values:
                continue
            flag = _coerce_bool(values[key])
            if flag is None:
                errors[key] = t("Expected on/off")
            else:
                accepted[key] = flag
        ignored = sorted(
            str(key) for key in values if key not in accepted and key not in errors
        )
        if errors:
            first = next(iter(errors.values()))
            return {"ok": False, "error": first, "errors": errors, "ignored": ignored}
        ok = True if not accepted else (_save_prefs(accepted) is not False)
        return {
            "ok": ok,
            "error": None if ok else t("Could not save the settings"),
            "ignored": ignored,
        }

    def get_settings(self) -> dict[str, Any]:
        """
        Everything the settings page renders (never raises).

        The proxy *password* is deliberately absent: the page only learns
        whether one is stored, and a new one is written only when the user
        types it.  A legacy ``user:pass@host`` value in ``general.proxy`` is
        also split: the address is returned without the userinfo and the
        username/password are reported through the fields that own them, so
        the page never round-trips a secret.  ``output_dir.state`` mirrors the
        boot payload so both screens agree on whether the folder is usable.
        """
        try:
            from novel_downloader.infra.i18n import SUPPORTED_LANGUAGES

            prefs = _load_prefs()
            data_dir = self.get_data_dir()
            proxy, inline_user, inline_password = _split_proxy_credentials(
                _configured_value("proxy")
            )
            return {
                "version": self._version(),
                "config_path": str(_settings_config_path()),
                "output_dir": {
                    **_directory_status(Path(data_dir)),
                    "configured": _configured_output_dir(),
                },
                "proxy": proxy,
                "proxy_user": _configured_value("proxy_user") or inline_user,
                "proxy_has_password": bool(
                    _configured_value("proxy_pass") or inline_password
                ),
                "auto_check_updates": _prefs_flag(prefs, "auto_check_updates", True),
                "auto_download_updates": _prefs_flag(
                    prefs, "auto_download_updates", True
                ),
                "auto_install_on_exit": _prefs_flag(
                    prefs, "auto_install_on_exit", True
                ),
                "search_filters": _stored_search_filters(prefs),
                "theme": _prefs_theme(prefs),
                "language": _active_language(),
                "languages": dict(SUPPORTED_LANGUAGES),
                "shortcut_supported": self.shortcut_supported(),
                "shortcut_exists": self.shortcut_exists(),
                "data_dir": data_dir,
            }
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Could not build the settings payload", exc_info=True)
            message = format_error(exc)
            return {
                "error": message,
                "output_dir": {
                    "path": "",
                    "state": "error",
                    "error": message,
                    "configured": "",
                },
                "proxy": "",
                "proxy_user": "",
                "proxy_has_password": False,
                "auto_check_updates": True,
                "auto_download_updates": True,
                "auto_install_on_exit": True,
                "search_filters": {},
                "theme": "light",
                "language": "",
                "languages": {},
                "shortcut_supported": False,
                "shortcut_exists": False,
                "data_dir": "",
            }

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """
        Validate and persist the settings page in one transaction-ish step.

        Every field is validated before anything is written, so an invalid
        proxy address cannot leave the output folder half-saved.  Never
        raises.

        :return: ``{"ok", "errors", "saved", "message", "settings"}`` plus a
            fresh ``i18n`` catalogue when the language changed.
        """
        try:
            return self._save_settings(values)
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.exception("Could not save the desktop settings")
            return {
                "ok": False,
                "errors": {"settings": format_error(exc)},
                "saved": [],
                "message": t("Could not save the settings"),
                "settings": self.get_settings(),
            }

    def _save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Implementation of :meth:`save_settings` (may raise on I/O bugs)."""
        from novel_downloader.infra.i18n import (
            SUPPORTED_LANGUAGES,
            get_language,
            normalize_language,
            set_language,
        )

        if not isinstance(values, dict):
            return {
                "ok": False,
                "errors": {"settings": t("Expected a settings object")},
                "saved": [],
                "message": t("Some settings were not saved"),
                "settings": self.get_settings(),
            }

        errors: dict[str, str] = {}
        config_updates: dict[str, Any] = {}
        pref_updates: dict[str, Any] = {}
        saved: list[str] = []

        if "theme" in values:
            theme = str(values.get("theme") or "").strip().lower()
            if theme in ("light", "dark"):
                pref_updates["theme"] = theme
            else:
                errors["theme"] = t("Unknown theme")

        for key in _PREF_BOOL_KEYS:
            if key not in values:
                continue
            flag = _coerce_bool(values[key])
            if flag is None:
                errors[key] = t("Expected on/off")
            else:
                pref_updates[key] = flag

        language = ""
        if "language" in values:
            requested = str(values.get("language") or "").strip()
            # An empty value means "leave unchanged" (the select can be empty
            # when the catalogue failed to load); it must not block the save.
            if requested:
                code = normalize_language(requested)
                if code in SUPPORTED_LANGUAGES:
                    language = code
                else:
                    errors["language"] = t("Unknown language")

        output_target: Path | None = None
        if "output_dir" in values:
            raw_path = str(values.get("output_dir") or "").strip()
            if not raw_path:
                errors["output_dir"] = t("Enter a folder path")
            else:
                # ``_expand_output_dir`` is total; the write probe is deferred
                # until every field validated, so a rejected proxy cannot
                # leave a freshly created (but never configured) folder.
                target = _expand_output_dir(raw_path)
                if target != _expand_output_dir(_configured_output_dir()):
                    output_target = target

        proxy_touched = any(
            key in values
            for key in (
                "proxy",
                "proxy_user",
                "proxy_password",
                "clear_proxy_password",
            )
        )
        if proxy_touched:
            stored_proxy = _configured_value("proxy")
            stored_user = _configured_value("proxy_user")
            stored_pass = _configured_value("proxy_pass")
            current_proxy, inline_user, inline_password = _split_proxy_credentials(
                stored_proxy
            )
            # Inline credentials count as the effective values; otherwise an
            # untouched save would look like "the user removed them".
            current_user = stored_user or inline_user
            current_pass = stored_pass or inline_password
            raw_proxy = values.get("proxy") if "proxy" in values else current_proxy
            proxy, proxy_error = _normalize_proxy(raw_proxy)
            if proxy_error is not None:
                errors["proxy"] = proxy_error
            else:
                user = (
                    str(values.get("proxy_user") or "").strip()
                    if "proxy_user" in values
                    else current_user
                )
                password = current_pass
                if str(values.get("proxy_password") or ""):
                    password = str(values["proxy_password"])
                elif _coerce_bool(values.get("clear_proxy_password")) is True:
                    password = ""
                if inline_user or inline_password:
                    # Migrate the inline secret into the keys the HTTP stack
                    # reads; leaving it in the URL leaks it to the page and
                    # drops it when the address changes.
                    config_updates["proxy"] = proxy
                    config_updates["proxy_user"] = user
                    config_updates["proxy_pass"] = password
                else:
                    if proxy != current_proxy:
                        config_updates["proxy"] = proxy
                    if user != current_user:
                        config_updates["proxy_user"] = user
                    if password != current_pass:
                        config_updates["proxy_pass"] = password

        if errors:
            return {
                "ok": False,
                "errors": errors,
                "saved": [],
                "message": t("Some settings were not saved"),
                "settings": self.get_settings(),
            }

        if output_target is not None:
            problem = _probe_directory(output_target)
            if problem is not None:
                errors["output_dir"] = (
                    problem
                    if problem == t("That path is a file, not a folder")
                    else t("The output folder could not be created: {error}").format(
                        error=problem
                    )
                )
                return {
                    "ok": False,
                    "errors": errors,
                    "saved": [],
                    "message": t("Some settings were not saved"),
                    "settings": self.get_settings(),
                }
            config_updates["output_dir"] = str(output_target)

        if config_updates:
            try:
                _update_config_section("general", config_updates)
            except Exception as exc:  # noqa: BLE001 - report, never crash
                logger.warning("Could not write the config file: %s", exc)
                field = "output_dir" if "output_dir" in config_updates else "proxy"
                return {
                    "ok": False,
                    "errors": {
                        field: t(
                            "Could not write the configuration file: {error}"
                        ).format(error=format_error(exc))
                    },
                    "saved": [],
                    "message": t("Some settings were not saved"),
                    "settings": self.get_settings(),
                }
            saved.extend(sorted(config_updates))

        if pref_updates:
            if _save_prefs(pref_updates) is False:
                return {
                    "ok": False,
                    "errors": {"prefs": t("Could not save the settings")},
                    "saved": saved,
                    "message": t("Some settings were not saved"),
                    "settings": self.get_settings(),
                }
            saved.extend(sorted(pref_updates))

        language_changed = False
        if language:
            try:
                if language != get_language():
                    set_language(language)
                    language_changed = True
                saved.append("language")
            except Exception as exc:  # noqa: BLE001 - keep the other settings
                logger.warning("Could not switch language: %s", exc)
                errors["language"] = format_error(exc)

        result: dict[str, Any] = {
            "ok": not errors,
            "errors": errors,
            "saved": saved,
            "message": t("Settings saved")
            if not errors
            else t("Some settings were not saved"),
            "settings": self.get_settings(),
        }
        if language_changed:
            result["i18n"] = _i18n_catalog()
        return result

    def open_path(self, path: str) -> bool:
        """
        Open a file or directory with the OS default handler.

        :return: ``True`` when the handler was launched.  A missing target or
            a shell failure reports ``False`` so callers that show feedback
            (the music page) can tell the user instead of failing silently.
        """
        target = str(path or "").strip()
        if not target or not Path(target).exists():
            return False
        try:
            if sys.platform == "win32":
                startfile = getattr(os, "startfile", None)
                if startfile is None:
                    return False
                startfile(target)  # noqa: S606 - user-initiated shell open
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])  # noqa: S603
            else:
                subprocess.Popen(["xdg-open", target])  # noqa: S603
        except OSError:
            logger.exception("Failed to open %s", target)
            return False
        return True

    def open_url(self, url: str) -> None:
        """Open an external URL in the default browser."""
        target = str(url or "").strip()
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)

    def open_downloads_dir(self) -> dict[str, Any]:
        """
        Open (creating it if needed) the configured export directory.

        Reports failures instead of returning ``None``: the page's "Open
        folder" button has no other way to tell the user why nothing happened.

        Never raises.

        :return: ``{"ok", "path", "error"}``.
        """
        out_dir = _expand_output_dir(_configured_output_dir())
        try:
            if not out_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create the download folder %s: %s", out_dir, exc)
            return {"ok": False, "path": str(out_dir), "error": format_error(exc)}
        resolved = str(out_dir)
        if not self.open_path(resolved):
            return {
                "ok": False,
                "path": resolved,
                "error": t("The system file manager could not open it"),
            }
        return {"ok": True, "path": resolved, "error": None}

    def open_music_dir(self) -> dict[str, Any]:
        """
        Open (creating it if needed) the directory music downloads land in.

        The page has no other way to find this location: it is the stored
        ``out_dir`` when set, otherwise the platform default.

        Never raises.

        :return: ``{"ok", "path", "error"}``.
        """
        try:
            out_dir = _music_output_dir()
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Could not resolve the music folder", exc_info=True)
            return {"ok": False, "path": "", "error": format_error(exc)}
        try:
            if not out_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create the music folder %s: %s", out_dir, exc)
            return {"ok": False, "path": str(out_dir), "error": format_error(exc)}
        resolved = str(out_dir.resolve())
        if not self.open_path(resolved):
            return {
                "ok": False,
                "path": resolved,
                "error": t("The system file manager could not open it"),
            }
        return {"ok": True, "path": resolved, "error": None}

    def play_chime(self) -> None:
        """Play the task-completion notification sound (best effort)."""
        try:
            from novel_downloader.apps.desktop.notifications import (
                play_notification_sound,
            )

            play_notification_sound()
        except Exception:  # noqa: BLE001 - sound is cosmetic
            logger.debug("Notification sound failed", exc_info=True)

    def check_update(self) -> dict[str, Any]:
        """
        Query GitHub for a newer release (throttled to once per 24 hours).

        Never raises: any failure — offline, rate-limited, bad payload —
        reports ``{"available": False}`` so the front end can stay quiet.

        The payload is interpreted by
        :func:`novel_downloader.apps.common.updater.parse_manifest`, which also
        selects this platform's asset.  Nothing is downloaded or executed
        here.

        :return: ``{"available", "latest", "url", "current", "message",
                 "platform", "asset_name"?, "asset_size"?, "sha256"?,
                 "throttled"?, "error"?}``.  The first five keys are always
                 present, whatever branch produced the answer.
        """
        prefs = _load_prefs()
        try:
            last = float(prefs.get("last_update_check") or 0.0)
        except (TypeError, ValueError):
            # A hand-edited preference file must not break the check.
            last = 0.0
        current = self._version()
        now = time.time()
        if now - last < _UPDATE_CHECK_INTERVAL:
            return {
                "available": False,
                "current": current,
                "latest": "",
                "url": _RELEASES_PAGE,
                "message": "",
                "throttled": True,
            }
        # The timestamp is written before the request: an offline machine must
        # not retry the (up to 8 s) fetch every time the panel is opened.
        _save_prefs({"last_update_check": str(now)})

        platform_tag = detect_platform()
        try:
            import json as _json
            import urllib.request

            req = urllib.request.Request(  # noqa: S310 - fixed https URL
                _RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "novel-downloader-desktop",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                payload = _json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("release payload is not a JSON object")
            info = parse_manifest(payload, current=current, platform_tag=platform_tag)
        except Exception as exc:  # noqa: BLE001 - offline is normal
            logger.debug("Update check failed: %s", exc)
            error = format_error(exc)
            self._remember_check(
                None,
                current=current,
                platform=platform_tag,
                checked_at=now,
                error=error,
            )
            return {
                "available": False,
                "current": current,
                "latest": "",
                "url": _RELEASES_PAGE,
                "message": "",
                "error": error,
                "platform": platform_tag,
            }

        # Keep the new ``update_state`` view in step with this legacy call.
        self._remember_check(
            info,
            current=current,
            platform=platform_tag,
            checked_at=now,
            error=None,
        )

        url = info.notes_url or _RELEASES_PAGE
        if not info.available:
            return {
                "available": False,
                "current": current,
                "latest": info.latest,
                "url": url,
                "message": "",
                "platform": platform_tag,
            }
        asset = info.asset
        return {
            "available": True,
            "current": current,
            "latest": info.latest,
            "url": url,
            "platform": platform_tag,
            "asset_name": asset.name if asset is not None else None,
            "asset_size": asset.size if asset is not None else None,
            "sha256": asset.sha256 if asset is not None else None,
            "message": t("Update available: v{ver}").replace("{ver}", info.latest),
        }

    # ------------------------------------------------------------------
    # updates: cached state, background check, download, install
    # ------------------------------------------------------------------

    def update_state(self) -> dict[str, Any]:
        """
        Return the cached update check, never touching the UI thread.

        When no check is recent enough and ``auto_check_updates`` allows it, a
        background check is kicked off; the (possibly empty) cache is returned
        immediately so the front end can render without waiting.  Turn the
        preference off with ``save_prefs({"auto_check_updates": false})`` to
        get cache-only behaviour with no network access.

        Never raises: a broken cache or platform probe degrades to an
        all-``None`` payload carrying the ``error`` text.

        :return: ``{"current", "latest", "available", "notes_url",
                 "asset_name", "asset_size", "platform", "checked_at",
                 "error", "pending", "auto_check"}``.
        """
        try:
            prefs = _load_prefs()
            now = time.time()
            with self._update_guard:
                cache = self._update_cache
                inflight = (
                    self._update_thread is not None and self._update_thread.is_alive()
                )
            auto = _prefs_flag(prefs, "auto_check_updates", True)
            if auto and not inflight and _update_due(prefs, now):
                self._start_update_check()
            return self._update_payload(cache, prefs)
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Could not serve the update state", exc_info=True)
            return self._empty_update_payload(format_error(exc))

    def check_update_async(self, force: bool = False) -> dict[str, Any]:
        """
        Start an update check on a background thread and return the cache.

        The throttle (24 h) and ``auto_check_updates`` preference are honoured
        unless ``force`` is set, so an automatic DOM-ready call can never
        hammer the release feed; ``force`` is for an explicit user action.

        Never raises, and never blocks: the caller polls :meth:`update_state`
        (``pending`` tells it when the result has landed).

        :param force: Ignore the throttle and the auto-check preference.
        :return: The :meth:`update_state` payload plus ``{"started", "reason"}``.
        """
        try:
            prefs = _load_prefs()
            now = time.time()
            with self._update_guard:
                cache = self._update_cache
                inflight = (
                    self._update_thread is not None and self._update_thread.is_alive()
                )
            payload = self._update_payload(cache, prefs)
            if inflight:
                return {**payload, "started": False, "reason": "pending"}
            if not force and not _prefs_flag(prefs, "auto_check_updates", True):
                return {**payload, "started": False, "reason": "disabled"}
            if not force and not _update_due(prefs, now):
                return {**payload, "started": False, "reason": "throttled"}
            started = self._start_update_check()
            if started:
                # The payload was built before the thread existed; report the
                # fresh state so the page knows it has to poll.
                payload = {**payload, "pending": True}
            return {
                **payload,
                "started": started,
                "reason": "" if started else "pending",
            }
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Could not start the update check", exc_info=True)
            return {
                **self._empty_update_payload(format_error(exc)),
                "started": False,
                "reason": "error",
            }

    def download_update(self) -> dict[str, Any]:
        """
        Download this platform's asset into the update folder.

        The file is fetched over HTTP(S) only, must match the digest published
        by the release, and is verified again once written.  A mismatch (or a
        file landing outside the update folder) deletes the file and reports a
        failure, so nothing unverified is ever left behind.

        Only one download may run at a time; a second concurrent call gets a
        friendly error instead of racing.

        Never raises.

        :return: ``{"ok", "path", "error"}`` plus ``asset_name`` on success.
        """
        try:
            return self._download_update()
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Update download failed", exc_info=True)
            return self._download_error(format_error(exc))

    def install_update(self) -> dict[str, Any]:
        """
        Launch the downloaded installer and close the app.

        Only a file this bridge downloaded *and* verified is accepted; the
        digest is checked again just before launch, so a tampered file is
        deleted rather than executed.  Windows only: elsewhere (and for a zip
        asset) the user is told to install manually — nothing is unpacked over
        a running program.

        Never raises.

        :return: ``{"ok", "error"}``; ``restarting`` is set when the installer
            was launched.
        """
        try:
            return self._install_update()
        except Exception as exc:  # noqa: BLE001 - bridge methods never raise
            logger.debug("Update install failed", exc_info=True)
            return {"ok": False, "error": format_error(exc)}

    def _announced_version(self) -> str:
        """The latest release the current cache advertises, or ``""``."""
        with self._update_guard:
            cache = self._update_cache
        info = cache.info if cache is not None else None
        if info is None or not info.available:
            return ""
        return _normalise_version(info.latest)

    def _version_conflict(self, record: _DownloadedUpdate) -> str:
        """The announced version when ``record`` is for an older release.

        :return: The newer version, or ``""`` when the record is usable (or
            predates version tracking, where nothing can be compared).
        """
        latest = self._announced_version()
        have = _normalise_version(record.version)
        if latest and have and latest != have:
            return latest
        return ""

    def _forget_download(
        self, record: _DownloadedUpdate, *, remove: bool = False
    ) -> None:
        """Drop a verified record (optionally deleting its file).

        Only the exact record is cleared: a newer download that landed in the
        meantime must never be discarded by a stale caller.
        """
        with self._download_guard:
            if self._downloaded is record:
                self._downloaded = None
                self._update_download_state = _idle_download_state()
        if remove:
            with contextlib.suppress(OSError):
                record.path.unlink(missing_ok=True)

    def _ready_record(self) -> _DownloadedUpdate | None:
        """The usable verified download, forgetting vanished/stale ones."""
        with self._download_guard:
            record = self._downloaded
        if record is None:
            return None
        try:
            present = record.path.is_file()
        except OSError:
            present = False
        if not present:
            # A pruned installer must flip the bar back to "download" instead
            # of dead-ending on an install that can only fail.
            self._forget_download(record)
            return None
        if self._version_conflict(record):
            self._forget_download(record, remove=True)
            return None
        return record

    def _has_downloaded_update(self) -> bool:
        """Whether a verified installer is waiting to be launched."""
        return self._ready_record() is not None

    def _downloaded_update_size(self) -> int:
        """Size of the verified update already on disk (0 when none)."""
        record = self._ready_record()
        if record is None:
            return 0
        try:
            return int(record.path.stat().st_size)
        except OSError:
            # The file vanished between the claim and the stat: report 0 and
            # let the install step surface the real error.
            self._forget_download(record)
            return 0

    def _install_on_exit(self) -> None:
        """Run a verified installer when the app is closing.

        This is the zero-click path: the update was downloaded in the
        background, and quitting the app is the only step left.  Only the
        release the current check advertises is launched — an older package
        left over from a previous check stays on disk for cleanup instead.
        """
        try:
            if sys.platform != "win32":
                return
            if not _prefs_flag(_load_prefs(), "auto_install_on_exit", True):
                return
            with self._download_guard:
                record = self._downloaded
            if record is None:
                return
            if self._version_conflict(record):
                return
            target = record.path
            suffix = target.suffix.lower()
            updates_dir = _updates_dir()
            if suffix not in {".exe", ".zip"} or not _inside_dir(target, updates_dir):
                return
            if not target.is_file():
                self._forget_download(record)
                return
            if not verify_sha256(target, record.sha256):
                self._forget_download(record, remove=True)
                return
            if suffix == ".exe" and detect_install_shape() == "portable":
                # Never run the Inno installer on a portable copy: it would
                # create a second installation rather than update this one.
                return
            with self._download_guard:
                if self._downloaded is not record:
                    return
                self._downloaded = None
            if suffix == ".zip":
                # A portable bundle needs the PowerShell replace-after-exit
                # helper; ``_launch_portable_update`` gives the claim back if
                # it cannot run, so a later cleanup can still see the file.
                self._launch_portable_update(record, updates_dir)
                return
            try:
                subprocess.Popen(  # noqa: S603
                    [str(target), *_INSTALLER_ARGS], close_fds=True, shell=False
                )
            except OSError:
                self._release_claim(record)
                raise
            logger.info("Launched the update installer on exit")
        except Exception:  # noqa: BLE001 - shutdown must never fail
            logger.debug("Install on exit failed", exc_info=True)

    def _on_window_closing(self) -> bool:
        """pywebview closing hook: install first, then let the window go."""
        self._install_on_exit()
        return True

    def _attach_window(self, window: Any) -> None:
        """
        Register the pywebview window so an update can close the app.

        :param window: The object returned by ``webview.create_window``.
        """

        def request_exit() -> None:
            logger.info("Closing the window so the installer can replace files")
            window.destroy()

        self._exit_callback = request_exit
        with contextlib.suppress(Exception):
            window.events.closing += self._on_window_closing

    def _remember_check(
        self,
        info: UpdateInfo | None,
        *,
        current: str,
        platform: str,
        checked_at: float,
        error: str | None,
    ) -> None:
        """Store one interpreted check for :meth:`update_state` to serve.

        A ready package for an older release is discarded here, at the same
        moment the newer release becomes known, so the bar can never offer an
        installer the feed no longer announces.
        """
        with self._update_guard:
            self._update_cache = _UpdateCache(
                info=info,
                error=error,
                checked_at=checked_at,
                current=current,
                platform=platform,
            )
        latest = ""
        if info is not None and info.available:
            latest = _normalise_version(info.latest)
        if not latest:
            return
        stale: _DownloadedUpdate | None = None
        with self._download_guard:
            record = self._downloaded
            have = _normalise_version(record.version) if record is not None else ""
            if record is not None and have and have != latest:
                stale = record
                self._downloaded = None
                # Reset the progress view too: a "ready" bar for v9.0.0 must
                # not stay green while v9.1.0 is the announced release.  A
                # transfer that is still running keeps its live progress; its
                # finished record is filtered out by ``_ready_record``.
                if not self._download_active:
                    self._update_download_state = _idle_download_state()
        if stale is not None:
            with contextlib.suppress(OSError):
                stale.path.unlink(missing_ok=True)

    def _empty_update_payload(self, error: str | None) -> dict[str, Any]:
        """The all-empty :meth:`update_state` payload for a failed read."""
        try:
            auto = _prefs_flag(_load_prefs(), "auto_check_updates", True)
        except Exception:  # noqa: BLE001 - this payload must never raise
            auto = True
        return {
            "current": self._version(),
            "latest": "",
            "available": False,
            "notes_url": _RELEASES_PAGE,
            # ``url`` mirrors ``notes_url``: the front end accepts either name
            # (the legacy ``check_update`` payload calls it ``url``), so both
            # views answer with the same key set.
            "url": _RELEASES_PAGE,
            "asset_name": None,
            "asset_size": None,
            "platform": "",
            "checked_at": 0.0,
            "error": error,
            "pending": False,
            "downloaded": False,
            "auto_check": auto,
            "auto_install": True,
        }

    def _update_payload(
        self,
        cache: _UpdateCache | None,
        prefs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Serialise a cache entry (or the empty state) for the page."""
        stored = _load_prefs() if prefs is None else prefs
        info = cache.info if cache is not None else None
        asset = info.asset if info is not None else None
        with self._update_guard:
            inflight = (
                self._update_thread is not None and self._update_thread.is_alive()
            )
        notes_url = (
            info.notes_url if info is not None and info.notes_url else _RELEASES_PAGE
        )
        return {
            "current": cache.current if cache is not None else self._version(),
            "latest": info.latest if info is not None else "",
            "available": bool(info is not None and info.available),
            "notes_url": notes_url,
            # Alias kept in step with ``notes_url``; see ``_empty_update_payload``.
            "url": notes_url,
            "asset_name": asset.name if asset is not None else None,
            "asset_size": asset.size if asset is not None else None,
            "platform": (cache.platform if cache is not None else _safe_platform_tag()),
            "checked_at": float(cache.checked_at) if cache is not None else 0.0,
            "error": cache.error if cache is not None else None,
            "pending": inflight,
            "downloaded": self._has_downloaded_update(),
            "downloaded_size": self._downloaded_update_size(),
            "auto_check": _prefs_flag(stored, "auto_check_updates", True),
            "auto_install": _prefs_flag(stored, "auto_install_on_exit", True),
        }

    def _start_update_check(self) -> bool:
        """
        Spawn the check thread unless one is already running.

        :return: ``True`` when a new thread was started.
        """
        with self._update_guard:
            if self._update_thread is not None and self._update_thread.is_alive():
                return False
            # Stamped before the request: an offline machine must not retry the
            # (up to 8 s) fetch on every poll of ``update_state``.
            _save_prefs({"last_update_check": str(time.time())})
            thread = threading.Thread(
                target=self._update_worker,
                name="novel-desktop-update",
                daemon=True,
            )
            self._update_thread = thread
        thread.start()
        return True

    def _update_worker(self) -> None:
        """Run one throttled check and publish the result to the cache."""
        current = self._version()
        platform_tag = _safe_platform_tag()
        try:
            check = check_for_update(
                current,
                slug=_updater().DEFAULT_REPO,
                platform_tag=platform_tag,
                shape=detect_install_shape(),
                timeout=_UPDATE_TIMEOUT,
            )
            cache = _UpdateCache(
                info=check.info,
                error=check.error,
                checked_at=check.checked_at,
                current=current,
                platform=platform_tag,
            )
        except Exception as exc:  # noqa: BLE001 - the worker must never die loudly
            logger.debug("Update check failed: %s", exc)
            cache = _UpdateCache(
                info=None,
                error=format_error(exc),
                checked_at=time.time(),
                current=current,
                platform=platform_tag,
            )
        with self._update_guard:
            self._update_cache = cache
            self._update_thread = None
        self._maybe_auto_download()

    def _maybe_auto_download(self) -> None:
        """Fetch a newly discovered update in the background.

        The bar's single button then only has to launch the installer, so
        the user never waits for a download.  Failures stay quiet: the
        button still downloads on demand.
        """
        try:
            if not _prefs_flag(_load_prefs(), "auto_download_updates", True):
                return
            with self._update_guard:
                cache = self._update_cache
            if cache is None or cache.info is None or not cache.info.available:
                return
            with self._download_guard:
                if self._downloaded is not None or self._download_active:
                    return
            threading.Thread(target=self._auto_download_worker, daemon=True).start()
        except Exception:  # noqa: BLE001 - never break the check worker
            logger.debug("Could not start the background download", exc_info=True)

    def _auto_download_worker(self) -> None:
        """Download the pending update without a user action."""
        try:
            result = self._download_update()
        except Exception:  # noqa: BLE001
            logger.debug("Background update download raised", exc_info=True)
            return
        if result.get("ok"):
            logger.info("Update downloaded in the background")
        else:
            logger.debug("Background update download failed: %s", result.get("error"))

    def _download_update(self, *, claimed: bool = False) -> dict[str, Any]:
        """Implementation of :meth:`download_update` (may raise).

        :param claimed: The caller already owns ``_download_active`` (the
            async entry point reserves it before spawning this worker).  A
            plain call claims it here and is rejected while another transfer
            is running.
        """
        if not claimed:
            with self._download_guard:
                busy = self._download_active
                if not busy:
                    self._download_active = True
            if busy:
                # Do not touch the shared progress dict: the worker that owns
                # the slot is still updating it, and a rejected second call
                # must not flip the bar to "failed" mid-transfer.
                return {
                    "ok": False,
                    "path": None,
                    "error": t("An update download is already running"),
                }
        try:
            with self._update_guard:
                cache = self._update_cache
            info = cache.info if cache is not None else None
            if info is None or not info.available:
                return self._download_error(t("No update is available to download"))
            version = _normalise_version(info.latest)
            asset = info.asset
            if asset is None:
                return self._download_error(
                    t("This release has no download for your platform")
                )
            if not _is_https_asset_url(asset.url):
                return self._download_error(
                    t("Refusing to download an update from an insecure source")
                )
            digest = str(asset.sha256 or "").strip()
            if not digest:
                return self._download_error(
                    t(
                        "The release does not publish a checksum; "
                        "refusing to download it"
                    )
                )

            updates_dir = _updates_dir()
            _prune_updates(updates_dir)
            name = Path(str(asset.name).replace("\\", "/")).name or "update.bin"
            with self._download_guard:
                self._update_download_state = {
                    "active": True,
                    "ok": False,
                    "received": 0,
                    "total": int(asset.size or 0),
                    "error": None,
                    "path": None,
                    "version": version or None,
                }

            def on_progress(received: int, total: int) -> None:
                # Called once per 64 KiB chunk; updating the shared dict is
                # cheap, but the page only polls twice a second, so there is
                # no need to touch anything else here.
                with self._download_guard:
                    state = self._update_download_state
                    state["received"] = int(received)
                    if total > 0:
                        state["total"] = int(total)

            try:
                written = download_asset(
                    asset.url,
                    Path(name),
                    expected_sha256=digest,
                    root=updates_dir,
                    timeout=_UPDATE_DOWNLOAD_TIMEOUT,
                    resume=True,
                    progress=on_progress,
                    attempts=3,
                )
            except (ValueError, OSError) as exc:
                logger.debug("Asset transfer failed: %s", exc)
                return self._download_error(format_error(exc))

            final = Path(written)
            if not _inside_dir(final, updates_dir):
                # Defensive: the shared helper already enforces this.
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove %s", final, exc_info=True)
                return self._download_error(
                    t("The download landed outside the update folder")
                )
            if not verify_sha256(final, digest):
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove %s", final, exc_info=True)
                return self._download_error(
                    t("The downloaded update failed its checksum check and was deleted")
                )

            with self._download_guard:
                self._downloaded = _DownloadedUpdate(
                    path=final,
                    sha256=digest,
                    name=final.name,
                    version=version or None,
                )
                self._update_download_state = {
                    "active": False,
                    "ok": True,
                    "received": int(final.stat().st_size),
                    "total": int(final.stat().st_size),
                    "error": None,
                    "path": str(final),
                    "version": version or None,
                }
            return {
                "ok": True,
                "path": str(final),
                "error": None,
                "asset_name": final.name,
            }
        finally:
            with self._download_guard:
                self._download_active = False
                self._update_download_state["active"] = False

    def _download_error(self, message: str) -> dict[str, Any]:
        """A failed :meth:`download_update` reply."""
        with self._download_guard:
            state = self._update_download_state
            self._update_download_state = {
                "active": False,
                "ok": False,
                "received": int(state.get("received") or 0),
                "total": int(state.get("total") or 0),
                "error": message,
                "path": None,
                "version": state.get("version"),
            }
        return {"ok": False, "path": None, "error": message}

    def update_download_state(self) -> dict[str, Any]:
        """
        Byte-level progress of the background update download.

        :return: ``{"active", "ok", "received", "total", "error", "path"}``;
            ``total`` is 0 when the server does not publish a length.
        """
        with self._download_guard:
            state = dict(self._update_download_state)
        try:
            received = int(state.get("received") or 0)
            total = int(state.get("total") or 0)
            state["percent"] = (
                max(0, min(100, int(received * 100 / total))) if total else 0
            )
        except (TypeError, ValueError):
            state["percent"] = 0
        return state

    def download_update_async(self) -> dict[str, Any]:
        """
        Start the update download on a worker thread and return immediately.

        The page polls :meth:`update_download_state` for bytes/percent, so the
        update bar stays responsive while a 60 MB asset is in flight.  A second
        call while a transfer is running reports ``started: False`` and
        ``active: True`` — without touching the running progress state.
        """
        version = self._announced_version()
        with self._download_guard:
            if self._download_active:
                # Deliberately leave the progress dict alone: a double click
                # must not erase the bytes the running worker already reported.
                return {
                    "started": False,
                    "active": True,
                    "error": t("An update download is already running"),
                }
            # Reserve the slot before spawning the worker.  It stays held
            # until the worker releases it in ``_download_update``'s finally
            # block, so a synchronous call cannot slip in between.
            self._download_active = True
            self._update_download_state = {
                "active": True,
                "ok": False,
                "received": 0,
                "total": 0,
                "error": None,
                "path": None,
                "version": version or None,
            }

        def worker() -> None:
            try:
                self._download_update(claimed=True)
            except Exception:  # noqa: BLE001 - surfaced through the state
                logger.debug("Update download raised", exc_info=True)
                self._download_error(t("Update download failed"))

        try:
            threading.Thread(
                target=worker, name="novel-desktop-update-dl", daemon=True
            ).start()
        except Exception:  # noqa: BLE001 - never leave the slot reserved
            self._download_error(t("Update download failed"))
            return {
                "started": False,
                "active": False,
                "error": t("Update download failed"),
            }
        return {"started": True, "active": True, "error": None}

    def _install_update(self) -> dict[str, Any]:
        """Implementation of :meth:`install_update` (may raise)."""
        with self._download_guard:
            record = self._downloaded
        if record is None:
            return {"ok": False, "error": t("No update has been downloaded yet")}
        announced = self._version_conflict(record)
        if announced:
            # The feed moved on after this file was fetched: never launch the
            # stale installer.  The record stays so a later pass can clean it.
            return {
                "ok": False,
                "error": t(
                    "The downloaded update is for v{have}, but v{want} is now "
                    "available; please download it again"
                )
                .replace("{have}", _normalise_version(record.version))
                .replace("{want}", announced),
            }
        # Claim the verified record atomically.  The page disables its buttons
        # while installing, but any script in the window can call the bridge
        # twice; without the claim both calls would reach ``Popen`` and start
        # two installers.  Every failure that leaves the file usable gives the
        # claim back (see :meth:`_release_claim`).
        with self._download_guard:
            if self._downloaded is not record:
                return {"ok": False, "error": t("No update has been downloaded yet")}
            self._downloaded = None
        try:
            return self._launch_update(record)
        except Exception:  # noqa: BLE001 - re-raised; the claim is returned first
            self._release_claim(record)
            raise

    def _launch_update(self, record: _DownloadedUpdate) -> dict[str, Any]:
        """Verify an already-claimed download and launch its installer."""
        if sys.platform != "win32":
            self._release_claim(record)
            return {
                "ok": False,
                "error": t(
                    "Automatic installation is only supported on Windows; "
                    "please extract the downloaded file manually"
                ),
            }
        target = record.path
        suffix = target.suffix.lower()
        if suffix not in {".exe", ".zip"}:
            self._release_claim(record)
            return {
                "ok": False,
                "error": t(
                    "This update is not an installer; please extract it manually"
                ),
            }
        if suffix == ".exe" and detect_install_shape() == "portable":
            # Running the Inno installer on a portable copy would create a
            # second installation instead of updating this folder.
            self._release_claim(record)
            return {
                "ok": False,
                "error": t(
                    "This update is an installer, which a portable copy cannot "
                    "use; download the portable package instead"
                ),
            }
        updates_dir = _updates_dir()
        if not _inside_dir(target, updates_dir):
            self._release_claim(record)
            return {
                "ok": False,
                "error": t("Refusing to run an update from outside the update folder"),
            }
        if not target.is_file():
            self._release_claim(record)
            return {
                "ok": False,
                "error": t(
                    "The downloaded installer is missing; please download it again"
                ),
            }
        if not verify_sha256(target, record.sha256):
            # The file changed after verification: never launch it.
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove %s", target, exc_info=True)
            return {
                "ok": False,
                "error": t(
                    "The downloaded installer failed its checksum check; "
                    "please download it again"
                ),
            }

        if suffix == ".zip":
            return self._launch_portable_update(record, updates_dir)

        try:
            # Fixed argument vector: no shell, no user-supplied text.  A list
            # argument keeps an installer path containing spaces intact
            # (``list2cmdline`` quotes it).  Setup closes this instance via
            # /CLOSEAPPLICATIONS and relaunches the new build from its [Run]
            # entry, so the process is never waited on here.
            process = subprocess.Popen(  # noqa: S603
                [str(target), *_INSTALLER_ARGS],
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
            # Nothing was started: hand the download back so the user can
            # retry without fetching the package again.
            logger.debug("Could not start the installer: %s", exc)
            self._release_claim(record)
            return {"ok": False, "error": format_error(exc)}

        pid = getattr(process, "pid", None)
        logger.info("Launched the update installer (pid=%s)", pid)
        # One install per download: the claim taken above stays consumed.
        self._schedule_exit()
        return {
            "ok": True,
            "error": None,
            "restarting": True,
            "pid": pid if isinstance(pid, int) else None,
        }

    def _launch_portable_update(
        self, record: _DownloadedUpdate, updates_dir: Path
    ) -> dict[str, Any]:
        """Replace this portable folder after the app exits, then restart it.

        A running executable cannot overwrite itself, so a small PowerShell
        helper waits for this process to end, expands the verified zip over the
        current folder and starts the new executable.  Only reachable for a
        ``portable`` install; the installer build keeps using Inno Setup.
        """

        def refuse(message: str) -> dict[str, Any]:
            """Give the download back so a failed attempt can be retried."""
            self._release_claim(record)
            return {"ok": False, "error": message}

        if detect_install_shape() != "portable":
            return refuse(
                t("This update is for a portable copy; use the installer instead")
            )
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return refuse(
                t(
                    "PowerShell was not found; please extract the downloaded "
                    "file over this folder manually"
                )
            )
        try:
            exe = Path(sys.executable).resolve()
            app_dir = exe.parent
        except OSError as exc:
            return refuse(format_error(exc))
        script = updates_dir / "_apply_portable_update.ps1"
        try:
            script.write_text(_PORTABLE_UPDATE_SCRIPT, encoding="utf-8")
        except OSError as exc:
            return refuse(format_error(exc))
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script),
            "-ProcessId",
            str(os.getpid()),
            "-Zip",
            str(record.path),
            "-AppDir",
            str(app_dir),
            "-Exe",
            exe.name,
        ]
        keep = _portable_keep_paths(app_dir)
        if keep:
            # Only paths inside _internal are ever at risk; the helper skips a
            # payload subtree containing user data rather than deleting it.
            command.extend(["-Keep", *keep])
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command, close_fds=True, shell=False
            )
        except OSError as exc:
            return refuse(format_error(exc))
        pid = getattr(process, "pid", None)
        logger.info("Launched the portable update helper (pid=%s)", pid)
        self._schedule_exit()
        return {
            "ok": True,
            "error": None,
            "restarting": True,
            "portable": True,
            "pid": pid if isinstance(pid, int) else None,
        }

    def _release_claim(self, record: _DownloadedUpdate) -> None:
        """Give a claimed download back when no installer was launched."""
        with self._download_guard:
            if self._downloaded is None:
                self._downloaded = record

    def _schedule_exit(self) -> None:
        """
        Close the window shortly after the installer has been launched.

        The short delay exists only so the ``install_update`` reply can cross
        the JS bridge; we never wait for (or inspect) the installer itself.
        Without a registered window (unit tests, headless use) nothing happens.

        pywebview runs every ``js_api`` method on a worker thread, so
        ``window.destroy()`` is called off the GUI thread on purpose.  The
        WinForms backend marshals it back through ``Control.Invoke``, which is
        the supported way to ask the UI thread to close a window.

        A close that raises is retried once; if it still fails the installer is
        already running while this process keeps its files open, so the failure
        is logged at warning level with an explicit "close the app manually"
        instruction instead of being swallowed.
        """
        callback = self._exit_callback
        if callback is None:
            return

        def close_once() -> bool:
            try:
                callback()
            except Exception:  # noqa: BLE001 - the process may be going away
                logger.debug("Could not close the window", exc_info=True)
                return False
            return True

        def run() -> None:
            time.sleep(_INSTALL_EXIT_DELAY)
            if close_once():
                return
            time.sleep(_INSTALL_EXIT_RETRY_DELAY)
            if close_once():
                return
            logger.warning(
                "The update installer was launched but the window could not be "
                "closed; please close the application manually so the installer "
                "can replace its files"
            )

        threading.Thread(
            target=run, name="novel-desktop-update-exit", daemon=True
        ).start()

    @staticmethod
    def shortcut_supported() -> bool:
        """Whether :meth:`create_shortcut` can succeed in this build."""
        return _shortcut_target() is not None and sys.platform == "win32"

    @staticmethod
    def shortcut_exists() -> bool:
        """Whether both shortcut targets already exist."""
        paths = _shortcut_paths()
        return bool(paths) and all(p.exists() for p in paths)

    def create_shortcut(self) -> dict[str, Any]:
        """
        Create desktop and Start-menu shortcuts (Windows, packaged app only).

        :return: ``{"ok", "message", "existed?"}`` for the front-end toast.
        """
        target = _shortcut_target()
        if target is None:
            return {
                "ok": False,
                "message": t("Shortcuts require the packaged app"),
            }
        if sys.platform != "win32":
            return {
                "ok": False,
                "message": t("Shortcuts are only supported on Windows"),
            }
        if self.shortcut_exists():
            return {
                "ok": True,
                "existed": True,
                "message": t("Shortcut already exists"),
            }
        script = _SHORTCUT_PS.format(
            target=target.replace("'", "''"), name=_SHORTCUT_NAME
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed args, no user input
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "message": format_error(exc)}
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:200]
            return {"ok": False, "message": format_error(RuntimeError(detail))}
        return {
            "ok": True,
            "message": t("Shortcuts created on the desktop and Start menu"),
        }

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def save_search_filters(
        self,
        min_words: int | float | str | None = None,
        max_words: int | float | str | None = None,
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """
        Persist the last valid search filters for the next launch.

        The same :func:`_parse_search_filters` contract as the search bridge
        applies, so an invalid value is rejected with the structured payload
        the page already knows how to display.  A set with every field blank
        clears the stored filters instead of writing an empty object.

        :return: ``{"ok", "filters", "error"}`` (plus ``error_code`` and
            ``error_field`` for a rejected value).  Never raises.
        """
        filters, error = _parse_search_filters(min_words, max_words, updated_after)
        if error is not None:
            return {**error, "filters": {}}
        stored = {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in filters.items()
        }
        if not stored:
            ok = _remove_prefs_keys("search_filters") is not False
        else:
            ok = _save_prefs({"search_filters": stored}) is not False
        return {
            "ok": ok,
            "filters": stored,
            "error": None if ok else t("Could not save the settings"),
        }

    def clear_search_filters(self) -> dict[str, Any]:
        """
        Forget the saved search filters (the settings-page action).

        :return: ``{"ok", "filters" (always empty), "error"}``.  Never raises.
        """
        ok = _remove_prefs_keys("search_filters") is not False
        return {
            "ok": ok,
            "filters": {},
            "error": None if ok else t("Could not save the settings"),
        }

    def search(
        self,
        keyword: str,
        sites_csv: str = "",
        group: bool = False,
        per_site_limit: int = 4,
        min_words: int | float | str | None = None,
        max_words: int | float | str | None = None,
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """
        Run an aggregate search and return ranked results.

        :param keyword: The search term.
        :param sites_csv: Optional comma-separated site filter.
        :param group: Merge same-book results into one entry per book, each
            carrying the ``sources`` capsules (best source first).
        :param per_site_limit: Results kept per site before ranking.
        :param min_words: Optional inclusive lower bound on the parsed word
            count; blank means no filter.
        :param max_words: Optional inclusive upper bound on the parsed word
            count; blank means no filter.
        :param updated_after: Optional inclusive lower bound on the update
            date, as ``YYYY-MM-DD``; blank means no filter.
        """
        text = str(keyword or "").strip()
        if not text:
            return {"results": [], "elapsed": 0.0}
        filters, error = _parse_search_filters(min_words, max_words, updated_after)
        if error is not None:
            return {**error, "results": [], "elapsed": 0.0}
        names = [s.strip() for s in str(sites_csv or "").split(",") if s.strip()]
        sites = names or None

        async def run() -> tuple[list[dict[str, Any]], float]:
            from novel_downloader.plugins.search import search

            started = time.perf_counter()
            results = await search(
                text,
                sites=sites,
                per_site_limit=per_site_limit,
                timeout=SEARCH_SITE_TIMEOUT,
                group=bool(group),
                **filters,
            )
            elapsed = time.perf_counter() - started
            return [_result_dict(r, i) for i, r in enumerate(results)], elapsed

        try:
            future = self._engine.submit(run())
            results, elapsed = future.result(SEARCH_WAIT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Aggregate search failed")
            return {"results": [], "elapsed": 0.0, "error": format_error(exc)}
        return {"results": results, "elapsed": elapsed}

    def search_async(
        self,
        keyword: str,
        sites_csv: str = "",
        group: bool = False,
        per_site_limit: int = 4,
        min_words: int | float | str | None = None,
        max_words: int | float | str | None = None,
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        """
        Start a progressive search and return its id.

        The page polls :meth:`search_progress` while the sites answer, so the
        first rows appear as soon as the fastest site replies instead of
        waiting for the slowest one. The final poll carries the fully ranked
        (and optionally merged) list. Every poll also reports how many sites
        completed or failed, the current row count and the elapsed time, which
        is what the page's progress line renders. :meth:`cancel_search` can
        stop the run early; late chunks are then discarded.

        :param keyword: The search term.
        :param sites_csv: Optional comma-separated site filter.
        :param group: Merge same-book results into one entry per book.
        :param per_site_limit: Results kept per site before ranking.
        :param min_words: Optional inclusive lower bound on the parsed word
            count; blank means no filter.
        :param max_words: Optional inclusive upper bound on the parsed word
            count; blank means no filter.
        :param updated_after: Optional inclusive lower bound on the update
            date, as ``YYYY-MM-DD``; blank means no filter.
        """
        text = str(keyword or "").strip()
        if not text:
            return {"search_id": "", "results": [], "done": True, "elapsed": 0.0}
        filters, error = _parse_search_filters(min_words, max_words, updated_after)
        if error is not None:
            return {
                **error,
                "search_id": "",
                "results": [],
                "done": True,
                "elapsed": 0.0,
            }
        names = [s.strip() for s in str(sites_csv or "").split(",") if s.strip()]
        sites = names or None

        search_id = uuid4().hex
        state: dict[str, Any] = {
            "results": [],
            "done": False,
            "cancelled": False,
            "error": "",
            "sites_done": 0,
            "sites_completed": 0,
            "sites_failed": 0,
            "result_count": 0,
            "elapsed": 0.0,
            "started": time.perf_counter(),
            # The concurrent future lets cancel_search stop the coroutine
            # (and with it the per-site tasks / HTTP session) instead of only
            # hiding the rows from the page.
            "future": None,
        }
        with self._lock:
            # Abandoned searches (page closed mid-run) must not pile up. A
            # replaced search is cancelled too, not left running in the
            # background for the whole per-site timeout.
            while len(self._searches) >= 8:
                old_key = next(iter(self._searches))
                old_state = self._searches.pop(old_key)
                old_future = old_state.get("future")
                if old_future is not None:
                    old_future.cancel()
            self._searches[search_id] = state

        async def run() -> None:
            from novel_downloader.plugins.search import search, search_stream

            rows: list[dict[str, Any]] = []
            stream: Any = None
            #: Sites already counted as finished, so a duplicate event cannot
            #: make "3/5 sources" jump past the real number of sites.
            settled: set[str] = set()
            #: Whether any per-site event arrived. A cache hit replays chunks
            #: without events, so only then may chunks stand in for sites.
            events_seen = False

            def on_event(event: Any) -> None:
                nonlocal events_seen
                kind = str(getattr(event, "kind", "") or "")
                if kind not in {"completed", "failed"}:
                    return
                site = str(getattr(event, "site", "") or "")
                with self._lock:
                    if bool(state["cancelled"]):
                        # Cancellation closes the stream; the cleanup failures
                        # it reports are not the user's search failing.
                        return
                    if site and site in settled:
                        return
                    events_seen = True
                    if site:
                        settled.add(site)
                    if kind == "completed":
                        state["sites_completed"] = int(state["sites_completed"]) + 1
                    else:
                        state["sites_failed"] = int(state["sites_failed"]) + 1
                    state["sites_done"] = int(state["sites_completed"]) + int(
                        state["sites_failed"]
                    )

            try:
                with self._lock:
                    if bool(state["cancelled"]):
                        return
                chunks = 0
                stream = search_stream(
                    text,
                    sites=sites,
                    per_site_limit=per_site_limit,
                    timeout=SEARCH_SITE_TIMEOUT,
                    on_event=on_event,
                    **filters,
                )
                try:
                    async for chunk in stream:
                        with self._lock:
                            if bool(state["cancelled"]):
                                break
                        base = len(rows)
                        rows.extend(
                            _result_dict(item, base + i) for i, item in enumerate(chunk)
                        )
                        chunks += 1
                        with self._lock:
                            state["results"] = list(rows)
                            state["result_count"] = len(rows)
                            # A cache hit replays chunks without per-site
                            # events; each chunk still means one finished
                            # site, so keep the progress counters honest.
                            if (
                                not events_seen
                                and int(state["sites_completed"]) < chunks
                            ):
                                delta = chunks - int(state["sites_completed"])
                                state["sites_completed"] = chunks
                                state["sites_done"] = int(state["sites_done"]) + delta
                finally:
                    # Stopping the stream is what closes its aiohttp session
                    # and cancels the site tasks; no process is killed.
                    try:
                        await stream.aclose()
                    except Exception:  # noqa: BLE001 - best effort cleanup
                        logger.debug(
                            "Closing a progressive search stream failed",
                            exc_info=True,
                        )

                with self._lock:
                    if bool(state["cancelled"]):
                        return
                if rows:
                    # The stream left its merged snapshot in the search cache,
                    # so this ranks/groups it without touching the network.
                    final = await search(
                        text,
                        sites=sites,
                        per_site_limit=per_site_limit,
                        timeout=SEARCH_SITE_TIMEOUT,
                        group=bool(group),
                        **filters,
                    )
                    with self._lock:
                        if bool(state["cancelled"]):
                            return
                    rows = [_result_dict(item, i) for i, item in enumerate(final)]
                with self._lock:
                    state["results"] = rows
                    state["result_count"] = len(rows)
            except Exception as exc:  # noqa: BLE001 - reported to the page
                with self._lock:
                    cancelled = bool(state["cancelled"])
                    if not cancelled:
                        state["error"] = format_error(exc)
                if cancelled:
                    logger.debug("Progressive search cancelled")
                else:
                    logger.exception("Progressive search failed")
            finally:
                with self._lock:
                    state["done"] = True
                    state["elapsed"] = time.perf_counter() - float(state["started"])
                    state["result_count"] = len(state["results"])

        def finalize(_future: Any) -> None:
            """Mark the snapshot final even when cancellation beats the task.

            Cancelling a coroutine before the loop picked it up means its own
            ``finally`` never runs; without this the page would poll a stale
            ``done=False`` entry forever.
            """
            with self._lock:
                state["done"] = True
                state["elapsed"] = time.perf_counter() - float(state["started"])
                state["result_count"] = len(state["results"])

        try:
            future = self._engine.submit(run())
        except Exception as exc:  # noqa: BLE001 - engine refused the coroutine
            logger.exception("Progressive search could not start")
            with self._lock:
                state["error"] = format_error(exc)
                state["done"] = True
        else:
            future.add_done_callback(finalize)
            with self._lock:
                state["future"] = future
                cancelled = bool(state["cancelled"])
            if cancelled:
                future.cancel()
        return {"search_id": search_id, "results": [], "done": False, "elapsed": 0.0}

    def cancel_search(self, search_id: str) -> dict[str, Any]:
        """
        Stop a progressive search and its per-site work.

        The search entry stays in ``_searches`` until the page polls the final
        ``done`` snapshot, so a cancelled search still reports the rows that
        had arrived. A new search id (or a finished one) is a harmless no-op.

        :return: ``{"ok": True, "search_id": ..., "cancelled": bool}``.
        """
        key = str(search_id or "")
        with self._lock:
            state = self._searches.get(key)
            if state is None:
                return {"ok": True, "search_id": key, "cancelled": False}
            if bool(state["done"]):
                return {"ok": True, "search_id": key, "cancelled": False}
            state["cancelled"] = True
            future = state.get("future")
        if future is not None:
            try:
                # Cancels the asyncio task on the engine loop, which unwinds
                # the stream (closing its HTTP session and site tasks).
                future.cancel()
            except Exception:  # noqa: BLE001 - already finished
                logger.debug("Could not cancel search %s", key, exc_info=True)
        return {"ok": True, "search_id": key, "cancelled": True}

    def search_progress(self, search_id: str) -> dict[str, Any]:
        """
        Return the rows accumulated for a progressive search.

        ``done`` marks the final poll, which carries the ranked list; the
        entry is dropped afterwards so a page that stops polling cannot leak
        state. ``cancelled`` tells the page that the partial rows are final
        because the user stopped the run.
        """
        key = str(search_id or "")
        with self._lock:
            state = self._searches.get(key)
            if state is None:
                return {
                    "results": [],
                    "done": True,
                    "cancelled": False,
                    "error": "",
                    "sites_done": 0,
                    "sites_completed": 0,
                    "sites_failed": 0,
                    "result_count": 0,
                    "elapsed": 0.0,
                }
            payload = {
                "results": list(state["results"]),
                "done": bool(state["done"]),
                "cancelled": bool(state["cancelled"]),
                "error": str(state["error"]),
                "sites_done": int(state["sites_done"]),
                "sites_completed": int(state["sites_completed"]),
                "sites_failed": int(state["sites_failed"]),
                "result_count": int(state["result_count"]),
                # Live polls report the wall time so far; the final poll has
                # the value frozen by the coroutine's cleanup.
                "elapsed": (
                    float(state["elapsed"])
                    if bool(state["done"])
                    else max(0.0, time.perf_counter() - float(state["started"]))
                ),
            }
            if payload["done"]:
                self._searches.pop(key, None)
        return payload

    def category_list(self) -> dict[str, Any]:
        """Return the browse categories (male/female groups)."""

        async def run() -> dict[str, Any]:
            import aiohttp

            from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
            from novel_downloader.plugins.sites.fanqienovel.searcher import (
                FanqienovelSearcher,
            )

            timeout_cfg = aiohttp.ClientTimeout(total=SEARCH_SITE_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout_cfg, headers=DEFAULT_USER_HEADERS
            ) as session:
                return await FanqienovelSearcher(session).categories()

        try:
            return {"groups": self._engine.submit(run()).result(SEARCH_WAIT_TIMEOUT)}
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.exception("Category list failed")
            return {"groups": {}, "error": format_error(exc)}

    def category_books(
        self, category_id: str, gender: str = "1", limit: int = 20
    ) -> dict[str, Any]:
        """Return the books of one browse category."""

        async def run() -> list[dict[str, Any]]:
            import aiohttp

            from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
            from novel_downloader.plugins.sites.fanqienovel.searcher import (
                FanqienovelSearcher,
            )

            timeout_cfg = aiohttp.ClientTimeout(total=SEARCH_SITE_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout_cfg, headers=DEFAULT_USER_HEADERS
            ) as session:
                rows = await FanqienovelSearcher(session).category_books(
                    str(category_id), gender=str(gender), limit=limit
                )
            return [_result_dict(r, i) for i, r in enumerate(rows)]

        try:
            rows = self._engine.submit(run()).result(SEARCH_WAIT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.exception("Category books failed")
            return {"results": [], "error": format_error(exc)}
        return {"results": rows}

    def rank_list(self, limit: int = 10) -> dict[str, Any]:
        """Return the 番茄 ranking page as result rows."""

        async def run() -> list[dict[str, Any]]:
            import aiohttp

            from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
            from novel_downloader.plugins.sites.fanqienovel.searcher import (
                FanqienovelSearcher,
            )

            timeout_cfg = aiohttp.ClientTimeout(total=SEARCH_SITE_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout_cfg, headers=DEFAULT_USER_HEADERS
            ) as session:
                rows = await FanqienovelSearcher(session).rank(limit)
            return [_result_dict(r, i) for i, r in enumerate(rows)]

        try:
            rows = self._engine.submit(run()).result(SEARCH_WAIT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.exception("Rank list failed")
            return {"results": [], "error": format_error(exc)}
        return {"results": rows}

    # ------------------------------------------------------------------
    # download tasks
    # ------------------------------------------------------------------

    def add_task(self, site: str, book_id: str, title: str = "") -> dict[str, Any]:
        """Queue one download; the front end polls for progress."""
        norm_site = str(site or "").strip()
        norm_id = str(book_id or "").strip()
        norm_title = str(title or "").strip() or norm_id
        if not norm_site or not norm_id:
            return {"ok": False, "error": t("Site and book ID are required")}

        async def run() -> Any:
            from novel_downloader.apps.core.task_manager import manager

            return await manager.add_task(
                title=norm_title, site=norm_site, book_id=norm_id
            )

        try:
            task = self._engine.submit(run()).result(TASK_WAIT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.exception("Failed to queue %s/%s", norm_site, norm_id)
            return {"ok": False, "error": format_error(exc)}
        return {"ok": True, "task_id": str(getattr(task, "task_id", ""))}

    def get_tasks(self) -> dict[str, list[dict[str, Any]]]:
        """Snapshot the queue, bucketed for the front end."""
        from novel_downloader.apps.core.task_manager import manager

        snap = manager.snapshot()
        live = [_task_dict(task) for task in snap.get("running", [])]
        queued = [_task_dict(task) for task in snap.get("pending", [])]
        # Defensive: a queued task observed in the running bucket (older
        # snapshots) is still reported as queued.
        live = [item for item in live if item["status"] != "queued"]
        return {
            "running": live,
            "queued": queued,
            "completed": [_task_dict(task) for task in snap.get("completed", [])],
        }

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Request cancellation of a queued or active task."""

        async def run() -> bool:
            from novel_downloader.apps.core.task_manager import manager

            return await manager.cancel_task(str(task_id or ""))

        try:
            ok = bool(self._engine.submit(run()).result(15.0))
        except Exception:  # noqa: BLE001 - state is re-polled anyway
            logger.exception("Cancel failed for %s", task_id)
            ok = False
        return {"ok": ok}

    def retry_task(self, task_id: str) -> dict[str, Any]:
        """Re-queue a failed or cancelled task from the completed list."""
        from novel_downloader.apps.core.task_manager import manager
        from novel_downloader.apps.desktop.formatting import retry_params

        target = next(
            (
                task
                for task in manager.snapshot().get("completed", [])
                if str(getattr(task, "task_id", "")) == str(task_id or "")
            ),
            None,
        )
        params = retry_params(target)
        if params is None:
            return {"ok": False}
        result = self.add_task(params.site, params.book_id, params.title)
        return {"ok": bool(result.get("ok"))}

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    def get_history(self) -> list[dict[str, Any]]:
        """List exported book files, newest first."""
        from novel_downloader.apps.common.history_files import _apply_sort, _scan_files

        items = _apply_sort(_scan_files(), "mtime", "desc")
        return [
            {
                "name": item.name,
                "ext": item.ext,
                "path": str(item.path),
                "size": item.size,
                "size_text": _format_size(item.size),
                "mtime": item.mtime,
                "mtime_text": _format_time(item.mtime),
            }
            for item in items
        ]

    # ------------------------------------------------------------------
    # follow list
    # ------------------------------------------------------------------

    def get_watchlist(self) -> list[dict[str, Any]]:
        """Serialise the follow list in stored order."""
        from novel_downloader.apps.core.watchlist import WatchlistStore
        from novel_downloader.apps.desktop.formatting import format_follow_updated

        entries = WatchlistStore().entries()
        return [
            {
                "site": entry.site,
                "book_id": entry.book_id,
                "title": entry.title,
                "last_chapter": entry.last_chapter,
                "updated_at": entry.updated_at,
                "updated_text": format_follow_updated(entry.updated_at),
            }
            for entry in entries
        ]

    def add_watch(self, site: str, book_id: str, title: str = "") -> dict[str, Any]:
        """Add a book to the follow list (or refresh an existing entry)."""
        norm_site = str(site or "").strip()
        norm_id = str(book_id or "").strip()
        norm_title = str(title or "").strip() or None
        if not norm_site or not norm_id:
            return {"ok": False, "message": t("Site and book ID are required")}
        try:
            from novel_downloader.apps.core.watchlist import WatchlistStore

            _entry, created = WatchlistStore().add(norm_site, norm_id, title=norm_title)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        except OSError as exc:
            logger.exception("Failed to write the follow list")
            return {"ok": False, "message": format_error(exc)}
        message = t("Added to follow list") if created else t("Already followed")
        return {"ok": True, "created": created, "message": message}

    def remove_watch(self, site: str, book_id: str) -> dict[str, Any]:
        """Remove one book from the follow list."""
        try:
            from novel_downloader.apps.core.watchlist import WatchlistStore

            removed = WatchlistStore().remove(str(site or ""), str(book_id or ""))
        except OSError:
            logger.exception("Failed to update the follow list")
            return {"ok": False}
        return {"ok": bool(removed)}

    def get_follow_check_state(self) -> dict[str, Any]:
        """
        Snapshot the current (or last) follow-list sweep.

        ``check_updates`` blocks the calling bridge thread until every book has
        been searched, so the page polls this method while it waits. The
        snapshot survives completion: it carries the per-book failures and the
        chapters that changed, which the aggregate counts cannot explain.

        :return: ``running``, ``total``, ``done``, ``updated``, ``failed``,
            ``error``, ``failures`` and ``updates``. Never raises.
        """
        with self._follow_guard:
            state = dict(self._follow_check)
            state["failures"] = [
                dict(item) for item in self._follow_check.get("failures", [])
            ]
            state["updates"] = [
                dict(item) for item in self._follow_check.get("updates", [])
            ]
        return state

    def check_updates(self) -> dict[str, Any]:
        """Check every followed book for a new chapter (blocking)."""
        with self._follow_guard:
            # The page disables its button, but a second caller (a script, a
            # double click racing the disable) must not start a parallel sweep
            # that would interleave the progress counters.
            if self._follow_check["running"]:
                return {
                    "checked": 0,
                    "updated": 0,
                    "failed": 0,
                    "error": t("A check is already running"),
                }
            self._follow_check.update(
                running=True,
                total=0,
                done=0,
                updated=0,
                failed=0,
                error=None,
                failures=[],
                updates=[],
            )

        def observe(result: Any) -> None:
            """Advance the live counters; the final pass rebuilds the lists."""
            with self._follow_guard:
                self._follow_check["done"] = int(self._follow_check["done"]) + 1
                if getattr(result, "error", None):
                    self._follow_check["failed"] = int(self._follow_check["failed"]) + 1

        async def run() -> list[Any]:
            from novel_downloader.apps.core.watchlist import (
                WatchlistStore,
                check_all,
            )

            store = WatchlistStore()
            entries = store.entries()
            with self._follow_guard:
                self._follow_check["total"] = len(entries)
            return list(await check_all(store, progress_cb=observe))

        try:
            results = self._engine.submit(run()).result(300.0)
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Follow check failed")
            message = format_error(exc)
            with self._follow_guard:
                self._follow_check.update(running=False, error=message)
            return {"checked": 0, "updated": 0, "failed": 0, "error": message}
        checked = len(results)
        updated = sum(1 for r in results if getattr(r, "changed", False))
        failed = sum(1 for r in results if getattr(r, "error", None))
        failures: list[dict[str, str]] = []
        updates: list[dict[str, str]] = []
        for result in results:
            entry = getattr(result, "entry", None)
            site = str(getattr(entry, "site", "") or "")
            book_id = str(getattr(entry, "book_id", "") or "")
            title = str(getattr(entry, "title", "") or "")
            error = getattr(result, "error", None)
            if error:
                failures.append(
                    {
                        "site": site,
                        "book_id": book_id,
                        "title": title,
                        "error": str(error),
                    }
                )
            elif getattr(result, "changed", False):
                updates.append(
                    {
                        "site": site,
                        "book_id": book_id,
                        "title": title,
                        "latest": str(getattr(result, "latest", "") or ""),
                    }
                )
        with self._follow_guard:
            self._follow_check.update(
                running=False,
                total=checked,
                done=checked,
                updated=updated,
                failed=failed,
                error=None,
                failures=failures,
                updates=updates,
            )
        return {"checked": checked, "updated": updated, "failed": failed}

    # ------------------------------------------------------------------
    # music
    # ------------------------------------------------------------------

    def get_music_platforms(self) -> list[dict[str, Any]]:
        """Available music platforms with grouped display labels."""
        funcs = _music_funcs()
        if funcs is None:
            return []
        return [
            {"key": key, "label": label, "note": funcs["platform_note"](key)}
            for key, label in funcs["available_labels"]()
        ]

    def get_music_platform_status(self) -> dict[str, Any]:
        """Guidance rows for heavy platforms, including unavailable ones.

        :return: ``{"ok": True, "platforms": [...]}`` or ``{"ok": False,
            "error": "..."}``; every row names the missing dependency/tool or
            the account the platform still needs.
        """
        try:
            return {"ok": True, "platforms": _music_platform_status()}
        except Exception as exc:  # noqa: BLE001 - the page needs an answer
            logger.exception("Could not build the music platform status")
            return {"ok": False, "error": format_error(exc)}

    def get_music_credentials(self) -> dict[str, Any]:
        """Credential rows for the settings card (stored values never included)."""
        try:
            return {"ok": True, "platforms": _music_credential_rows()}
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            logger.exception("Could not read the music credentials")
            return {"ok": False, "error": format_error(exc)}

    def save_music_credentials(self, platform: str, value: str) -> dict[str, Any]:
        """Store a platform credential JSON blob; blank leaves it unchanged.

        The raw value is never logged or echoed back. Clearing is explicit
        (:meth:`clear_music_credentials`) so an accidental blank field cannot
        destroy a working token.
        """
        key = str(platform or "").strip()
        if key not in _MUSIC_CREDENTIAL_KEYS:
            return {
                "ok": False,
                "error": t("Unknown music platform: {platform}").replace(
                    "{platform}", key
                ),
            }
        text = str(value or "").strip()
        if not text:
            return {
                "ok": True,
                "changed": False,
                "configured": _music_credential_configured(key),
            }
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"ok": False, "error": t("Platform credentials must be valid JSON.")}
        if not isinstance(parsed, dict) or not parsed:
            return {
                "ok": False,
                "error": t("Platform credentials must be a non-empty JSON object."),
            }
        try:
            from novel_downloader.apps.music.providers import creds

            creds.set(key, parsed)  # type: ignore[no-untyped-call]
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Could not save the music credentials for %s", key)
            return {
                "ok": False,
                "error": t("Could not save the platform credentials: {error}").replace(
                    "{error}", format_error(exc)
                ),
            }
        return {"ok": True, "changed": True, "configured": True}

    def clear_music_credentials(self, platform: str) -> dict[str, Any]:
        """Remove a stored platform credential."""
        key = str(platform or "").strip()
        if key not in _MUSIC_CREDENTIAL_KEYS:
            return {
                "ok": False,
                "error": t("Unknown music platform: {platform}").replace(
                    "{platform}", key
                ),
            }
        try:
            from novel_downloader.apps.music.providers import creds

            creds.clear(key)  # type: ignore[no-untyped-call]
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Could not clear the music credentials for %s", key)
            return {
                "ok": False,
                "error": t("Could not clear the platform credentials: {error}").replace(
                    "{error}", format_error(exc)
                ),
            }
        return {"ok": True, "configured": False}

    def music_note(self, platform: str) -> str:
        """Usage hint for one platform, or for the aggregate search (may be empty)."""
        funcs = _music_funcs()
        if funcs is None:
            return ""
        if funcs["is_all_platforms"](platform):
            return t("Searches every platform at once and merges the results")
        return str(funcs["platform_note"](str(platform or "")) or "")

    def music_search(
        self, platform: str, keyword: str, limit: int = 30
    ) -> dict[str, Any]:
        """
        Search one platform, or every platform when ``platform`` is ``"all"``.

        The aggregate answer carries one merged row per song: ``sources`` lists
        the platforms it was found on. Both shapes return a ``search_id`` that
        :meth:`music_download` uses to recover the original (non-JSON) tracks.
        """
        funcs = _music_funcs()
        if funcs is None:
            return {"tracks": [], "error": t("Music extras are not installed")}
        text = str(keyword or "").strip()
        if not text:
            return {"tracks": []}
        try:
            wanted = max(1, int(limit))
            if funcs["is_all_platforms"](platform):
                result = funcs["search_all"](text, None, wanted)
                tracks = list(result.tracks)
                return {
                    "tracks": [_music_track_dict(track) for track in tracks],
                    "search_id": funcs["CACHE"].put(tracks),
                    "platforms": [item.as_dict() for item in result.platforms],
                    "platform": funcs["ALL_PLATFORM_KEY"],
                    "total": int(result.total),
                }
            provider = funcs["create"](str(platform or ""))
            found = provider.search(text, limit=wanted)
            # Providers answer ``(tracks, total)``; older fakes return a list.
            tracks = list(found[0] if isinstance(found, tuple) else found or ())
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Music search failed on %s", platform)
            return {"tracks": [], "error": format_error(exc)}
        return {
            "tracks": [_music_track_dict(track) for track in tracks],
            "search_id": funcs["CACHE"].put(tracks),
        }

    def music_search_async(
        self, platform: str, keyword: str, limit: int = 30
    ) -> dict[str, Any]:
        """
        Start an aggregate search and return the id the page polls.

        Aggregating 20+ platforms takes several seconds, so waiting for the
        slowest one made the page look frozen and often empty. Here every
        platform's arrival updates the shared state (``on_update``), and the
        front end redraws while the rest are still running.

        A concrete platform is answered synchronously (providers are fast);
        its state is stored as already done so both paths poll the same way.

        :param platform: ``"all"``/empty for every platform, else a platform key.
        :param keyword: The search term.
        :param limit: Maximum number of merged rows returned.
        """
        funcs = _music_funcs()
        text = str(keyword or "").strip()
        try:
            wanted = max(1, int(limit))
        except (TypeError, ValueError):
            wanted = 30

        search_id = uuid4().hex
        state: dict[str, Any] = {
            "tracks": [],
            "platforms": [],
            "done": False,
            "error": "",
            "elapsed": 0.0,
            "result_id": "",
            "partial": False,
            "started": time.perf_counter(),
        }
        with self._lock:
            self._store_music_search(search_id, state)

        if funcs is None:
            with self._lock:
                state["error"] = t("Music extras are not installed")
                state["done"] = True
                state["elapsed"] = 0.0
            return {"search_id": search_id, "done": True, "error": state["error"]}

        if not text:
            with self._lock:
                state["done"] = True
            return {"search_id": search_id, "done": True}

        if not funcs["is_all_platforms"](platform):
            # One platform: the existing synchronous call is quick and keeps
            # the single-platform error contract (the page may toast it).
            result = self.music_search(platform, text, wanted)
            with self._lock:
                state["tracks"] = list(result.get("tracks") or [])
                state["error"] = str(result.get("error") or "")
                state["result_id"] = str(result.get("search_id") or "")
                state["done"] = True
                state["elapsed"] = time.perf_counter() - float(state["started"])
            return {
                "search_id": search_id,
                "done": True,
                "tracks": state["tracks"],
                "result_id": state["result_id"],
                "error": state["error"],
            }

        def on_update(partial: Any) -> None:
            """Receive one aggregate snapshot (called from search threads)."""
            tracks = [_music_track_dict(track) for track in list(partial.tracks or ())]
            platforms = [item.as_dict() for item in list(partial.platforms or ())]
            with self._lock:
                # Late callbacks from a cancelled/timed-out search must not
                # overwrite the final, cache-backed result.
                if state["done"]:
                    return
                state["tracks"] = tracks
                state["platforms"] = platforms
                state["partial"] = bool(platforms)
                state["elapsed"] = time.perf_counter() - float(state["started"])

        def work() -> None:
            try:
                result = funcs["search_all"](
                    text,
                    None,
                    wanted,
                    timeout=SEARCH_SITE_TIMEOUT,
                    on_update=on_update,
                )
                tracks = list(result.tracks)
                result_id = str(funcs["CACHE"].put(tracks))
                with self._lock:
                    state["tracks"] = [_music_track_dict(track) for track in tracks]
                    state["platforms"] = [
                        item.as_dict() for item in list(result.platforms or ())
                    ]
                    state["result_id"] = result_id
                    state["error"] = ""
                    # Mark done in the same critical section: a late platform
                    # thread calling ``on_update`` afterwards must not replace
                    # the rows the cache was built from.
                    state["done"] = True
                    state["partial"] = False
            except Exception as exc:  # noqa: BLE001 - reported through the poll
                logger.exception("Aggregate music search crashed")
                with self._lock:
                    state["error"] = format_error(exc)
            finally:
                with self._lock:
                    state["done"] = True
                    state["partial"] = False
                    state["elapsed"] = time.perf_counter() - float(state["started"])

        threading.Thread(
            target=work, name="novel-desktop-music-search", daemon=True
        ).start()
        return {"search_id": search_id, "done": False}

    def _store_music_search(self, search_id: str, state: dict[str, Any]) -> None:
        """Remember a progressive search, evicting the oldest one (lock held)."""
        while len(self._music_searches) >= MUSIC_SEARCH_LIMIT:
            self._music_searches.pop(next(iter(self._music_searches)), None)
        self._music_searches[search_id] = state

    def music_search_progress(self, search_id: str) -> dict[str, Any]:
        """
        Poll a progressive music search.

        The snapshot is copied under the lock so the page can redraw while the
        worker keeps updating the shared state. Unknown ids (evicted, or a
        page reload) answer ``done`` with no rows instead of raising.
        """
        key = str(search_id or "")
        with self._lock:
            state = self._music_searches.get(key)
            if state is None:
                return {
                    "done": True,
                    "tracks": [],
                    "platforms": [],
                    "result_id": "",
                    "error": "",
                    "elapsed": 0.0,
                    "partial": False,
                }
            return {
                "done": bool(state["done"]),
                "tracks": list(state["tracks"]),
                "platforms": list(state["platforms"]),
                "result_id": str(state["result_id"]),
                "error": str(state["error"]),
                "elapsed": float(state["elapsed"]),
                "partial": bool(state["partial"]),
            }

    def music_download(
        self,
        platform: str,
        tracks: list[dict[str, Any]],
        level: str | None = None,
        search_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Start downloading the selected tracks on a background thread.

        ``platform="all"`` routes every track to its own provider (aggregate
        search); an explicit key keeps the single-platform behaviour. When
        ``search_id`` is given the original, in-process tracks are restored from
        the search cache, because the serialised copies cannot be resolved.

        Progress is polled through :meth:`get_music_status`; the returned
        dictionary only reports whether the job started.
        """
        with self._lock:
            if self._music_active:
                return {"ok": False, "error": t("A download is already running")}
            self._music_active = True
            self._music_done = 0
            self._music_total = len(tracks)
            self._music_fraction = 0.0
            self._music_summary = ""
            self._music_failed = False
            self._music_paths = [""] * len(tracks)
            self._music_results = [{} for _ in tracks]
            self._music_failed_items = []
            self._music_stop.clear()

        worker = threading.Thread(
            target=self._music_worker,
            args=(
                str(platform or ""),
                list(tracks or []),
                level,
                str(search_id or "").strip() or None,
            ),
            name="novel-desktop-music",
            daemon=True,
        )
        worker.start()
        return {"ok": True}

    def _music_worker(
        self,
        platform: str,
        tracks: list[dict[str, Any]],
        level: str | None,
        search_id: str | None = None,
    ) -> None:
        """Download tracks on a worker thread, updating polled state."""
        # ``download_mixed`` invokes the callbacks from its own worker pool, so
        # every counter is updated under the bridge lock: an unlocked ``+=``
        # loses results whenever two tracks finish concurrently.
        counts = {"ok": 0, "skip": 0, "fail": 0}
        # index in the submitted batch -> why it failed; a successful fallback
        # source later clears its entry (``download_mixed`` re-reports it).
        failed: dict[int, dict[str, Any]] = {}
        # indexes whose audio landed with a ``.lrc`` beside it; drives the
        # "lyrics saved" part of the batch summary.
        lyric_indexes: set[int] = set()
        try:
            funcs = _music_funcs()
            if funcs is None:
                raise RuntimeError(t("Music extras are not installed"))
            options = self._music_options(level)
            track_cls = funcs["Track"]
            fields = {f.name for f in dataclasses.fields(track_cls)}
            objects = [
                track_cls(**{k: v for k, v in raw.items() if k in fields})
                for raw in tracks
                if isinstance(raw, dict)
            ]
            if search_id:
                cache = funcs["CACHE"]
                if cache.get(search_id) is None:
                    # The cache expires (or dies with the process): say so
                    # instead of letting every track fail with a cryptic error.
                    raise RuntimeError(
                        t(
                            "The search results expired — search again "
                            "before downloading"
                        )
                    )
                objects = cache.resolve(search_id, objects)
            with self._lock:
                self._music_total = len(objects)

            if funcs["is_all_platforms"](platform):
                create_provider = funcs["create"]
                target_platform = None
            else:
                fixed = funcs["create"](platform)

                def create_provider(_key: str, _provider: Any = fixed) -> Any:
                    """Every track uses the platform picked in the UI."""
                    return _provider

                target_platform = platform

            def on_result(index: int, _track: object, result: object) -> None:
                state = str(getattr(result, "status", "") or "").lower()
                key = "ok" if state == "ok" else "skip" if state == "skip" else "fail"
                path = str(
                    getattr(_track, "path", "") or getattr(result, "path", "") or ""
                )
                reason = str(getattr(result, "reason", "") or "")
                name = str(getattr(_track, "name", "") or "")
                # Only report files that really exist: the page turns this
                # into "open lyrics" buttons, and a stale path would make
                # those buttons a silent no-op.
                lyric_entries = _music_lyric_paths(path)
                # ``lyric_path`` stays the original ``.lrc`` for compatibility
                # (the summary and older pages count only that file).
                lyric_path = next(
                    (
                        entry["path"]
                        for entry in lyric_entries
                        if entry["lang"] == _LYRIC_LANG_ORIGINAL
                    ),
                    "",
                )
                entry: dict[str, Any] = {}
                if path:
                    entry["path"] = path
                if lyric_path:
                    entry["lyric_path"] = lyric_path
                if lyric_entries:
                    entry["lyric_paths"] = lyric_entries
                with self._lock:
                    counts[key] += 1
                    # Exactly one increment per finished track: the count is
                    # owned here, ``on_tick`` only drives the fraction.
                    self._music_done += 1
                    if 0 <= index < len(self._music_paths) and path:
                        self._music_paths[index] = path
                    if 0 <= index < len(self._music_results):
                        self._music_results[index] = entry
                    if key == "fail":
                        lyric_indexes.discard(index)
                        failed[index] = {"index": index, "name": name, "reason": reason}
                    else:
                        # A fallback source succeeded (or a retry landed):
                        # the row is no longer a failure.
                        failed.pop(index, None)
                        if lyric_path:
                            lyric_indexes.add(index)
                        else:
                            lyric_indexes.discard(index)

            def on_tick(fraction: float, _done: int) -> None:
                with self._lock:
                    # Worker threads report snapshots that can arrive out of
                    # order; keep the most advanced one so the progress bar
                    # never moves backwards.
                    self._music_fraction = max(self._music_fraction, float(fraction))

            funcs["download_mixed"](
                create_provider,
                objects,
                options,
                workers=3,
                on_result=on_result,
                on_tick=on_tick,
                should_stop=self._music_stop.is_set,
                platform=target_platform,
            )
        except Exception as exc:  # noqa: BLE001 - reported through the poll
            logger.exception("Music download crashed")
            reason = format_error(exc)
            with self._lock:
                counts["fail"] += 1
                self._music_failed = True
                self._music_summary = reason
                if not failed and not self._music_done:
                    # Nothing ran (expired search id, missing extras): every
                    # submitted row failed for the same reason, so the retry
                    # panel still has something to offer.
                    for index, raw in enumerate(tracks):
                        name = (
                            str(raw.get("name") or "") if isinstance(raw, dict) else ""
                        )
                        failed[index] = {
                            "index": index,
                            "name": name,
                            "reason": reason,
                        }
        finally:
            with self._lock:
                self._music_active = False
                self._music_fraction = 1.0
                self._music_failed_items = [failed[index] for index in sorted(failed)]
                if not self._music_summary:
                    if counts["fail"] == 0 and counts["skip"] == 0:
                        self._music_summary = t("Downloaded {n} tracks").replace(
                            "{n}", str(counts["ok"])
                        )
                    else:
                        self._music_summary = (
                            t("Done: {ok} downloaded, {skip} skipped, {fail} failed")
                            .replace("{ok}", str(counts["ok"]))
                            .replace("{skip}", str(counts["skip"]))
                            .replace("{fail}", str(counts["fail"]))
                        )
                    if lyric_indexes:
                        # The batch summary is the only place the user learns
                        # how many tracks got a ``.lrc`` without opening every
                        # song; keep the zero case out so old runs read the same.
                        self._music_summary += " · " + t("{n} with lyrics").replace(
                            "{n}", str(len(lyric_indexes))
                        )
                self._music_failed = self._music_failed or counts["fail"] > 0

    @staticmethod
    def _music_options(level: str | None) -> Any:
        """Build :class:`DownloadOptions` from the stored music settings."""
        from novel_downloader.apps.music.settings import load_settings

        stored = load_settings()
        return _music_options_type(
            out_dir=str(stored.get("out_dir") or ""),
            level=str(level or stored.get("level") or "exhigh"),
            embed_tags=bool(stored.get("embed_tags", True)),
            embed_cover=bool(stored.get("embed_cover", True)),
            save_lyric=bool(stored.get("save_lyric", True)),
            split_folder=bool(stored.get("split_folder", True)),
            skip_existing=bool(stored.get("skip_existing", True)),
        )

    def get_music_status(self) -> dict[str, Any]:
        """Polled music download progress."""
        with self._lock:
            return {
                "active": self._music_active,
                "done": self._music_done,
                "total": self._music_total,
                "fraction": self._music_fraction,
                "summary": self._music_summary,
                "failed": self._music_failed,
                "paths": list(self._music_paths),
                # Aligned with ``paths``: each item may carry ``path``, the
                # legacy ``lyric_path`` (the original ``.lrc`` the music engine
                # saved next to the audio, when it exists) and ``lyric_paths``
                # (every existing lyric file, original first).
                "results": [dict(item) for item in self._music_results],
                # Indexes refer to the track list handed to the most recent
                # ``music_download`` call, in that order.
                "failed_items": [dict(item) for item in self._music_failed_items],
            }

    def music_stop(self) -> dict[str, Any]:
        """Ask the running music download to stop."""
        self._music_stop.set()
        return {"ok": True}

    def music_export(
        self,
        platform: str,
        tracks: list[dict[str, Any]],
        out_path: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Write an M3U8 playlist for tracks that already have a local file.

        Mirrors ``POST /api/v1/music/export``: tracks without ``path`` are
        counted in ``skipped`` and never fail the request; when none of them is
        playable the caller gets an explicit error instead of an empty file.
        `platform` is accepted for parity with the REST contract and may be
        ``"all"`` (aggregate results): the playlist itself only stores file
        locations, so no per-platform filtering happens here.

        ``out_path`` must end in ``.m3u`` / ``.m3u8``; anything else is treated
        as a directory (see :func:`_playlist_target`). An existing target is
        only replaced when ``overwrite`` is true: otherwise the answer carries
        ``needs_overwrite`` so the UI can ask first.
        """
        funcs = _music_funcs()
        if funcs is None:
            return {"ok": False, "error": t("Music extras are not installed")}
        track_cls = funcs["Track"]
        fields = {field.name for field in dataclasses.fields(track_cls)}
        ready: list[Any] = []
        skipped = 0
        for raw in tracks if isinstance(tracks, list) else []:
            path = str(raw.get("path") or "").strip() if isinstance(raw, dict) else ""
            if not path:
                skipped += 1
                continue
            ready.append(track_cls(**{k: v for k, v in raw.items() if k in fields}))
        if not ready:
            return {
                "ok": False,
                "error": t(
                    "No track has a local file path yet — download the tracks first"
                ),
                "skipped": skipped,
            }

        target = _playlist_target(out_path)
        if target.exists() and not overwrite:
            # Never clobber a playlist the user may still want: the caller gets
            # the path (and ``needs_overwrite``) so it can ask for consent.
            return {
                "ok": False,
                "error": t("The playlist already exists: {path}").replace(
                    "{path}", str(target)
                ),
                "path": str(target),
                "needs_overwrite": True,
                "skipped": skipped,
            }
        try:
            written = funcs["export_m3u"](ready, target)
        except OSError as exc:
            # This is the user's own destination, so the rendering helper (not
            # a raw traceback) is shown; the full reason goes to the log. The
            # path is part of the message because the caller picked it.
            logger.warning("Playlist write failed for %s: %s", target, exc)
            return {
                "ok": False,
                "error": t("Could not write the playlist to {path}: {error}")
                .replace("{path}", str(target))
                .replace("{error}", format_error(exc)),
                "path": str(target),
            }
        return {
            "ok": True,
            "path": str(written),
            "tracks_written": len(ready),
            "skipped": skipped,
        }

    def get_music_settings(self) -> dict[str, Any]:
        """Stored music settings for the settings panel."""
        from novel_downloader.apps.music.settings import load_settings

        stored = load_settings()
        return {
            "level": str(stored.get("level") or "exhigh"),
            "embed_tags": bool(stored.get("embed_tags", True)),
            "embed_cover": bool(stored.get("embed_cover", True)),
            "save_lyric": bool(stored.get("save_lyric", True)),
            "split_folder": bool(stored.get("split_folder", True)),
            "skip_existing": bool(stored.get("skip_existing", True)),
        }

    def save_music_settings(self, values: dict[str, Any]) -> None:
        """Merge the settings panel back into the music settings file."""
        if not isinstance(values, dict):
            return
        try:
            from novel_downloader.apps.music.settings import (
                load_settings,
                save_settings,
            )

            stored = load_settings()
            stored.update(values)
            save_settings(stored)
        except Exception:  # noqa: BLE001 - settings are best effort
            logger.exception("Failed to save music settings")

    def music_health_start(self) -> dict[str, Any]:
        """Probe every platform once, in parallel, for the health panel.

        :return: ``{"ok", "error"}``; ``error`` explains a refusal (missing
            music extras) so the page can show it instead of leaving the
            button looking dead.
        """
        funcs = _music_funcs()
        if funcs is None:
            return {"ok": False, "error": t("Music extras are not installed")}
        with self._lock:
            if self._health_thread is not None and self._health_thread.is_alive():
                return {"ok": False, "error": ""}
            labels = dict(funcs["available_labels"]())
            self._health_rows = {
                key: {
                    "key": key,
                    "label": label,
                    "done": False,
                    "ok": False,
                    "error": "",
                    "elapsed_ms": 0,
                }
                for key, label in labels.items()
            }

            def worker() -> None:
                gate = threading.Semaphore(HEALTH_WORKERS)

                def probe(key: str) -> None:
                    with gate:
                        try:
                            result = funcs["health_check"](
                                key, timeout=HEALTH_CHECK_TIMEOUT
                            )
                        except Exception as exc:  # noqa: BLE001 - per-platform
                            result = {
                                "ok": False,
                                "elapsed_ms": 0,
                                "error": format_error(exc),
                            }
                        with self._lock:
                            row = self._health_rows.get(key)
                            if row is not None:
                                row["done"] = True
                                row["ok"] = bool(result.get("ok"))
                                row["error"] = str(result.get("error") or "")
                                row["elapsed_ms"] = int(result.get("elapsed_ms") or 0)

                threads = [
                    threading.Thread(target=probe, args=(key,), daemon=True)
                    for key in labels
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(45.0)

            self._health_thread = threading.Thread(
                target=worker, name="novel-desktop-health", daemon=True
            )
            self._health_thread.start()
        return {"ok": True}

    def get_music_health(self) -> list[dict[str, Any]]:
        """Current health-panel rows (in platform order)."""
        with self._lock:
            return [
                {
                    "key": row["key"],
                    "label": row["label"],
                    "done": row["done"],
                    "ok": row["ok"],
                    "error": row["error"],
                    "elapsed_text": (
                        _format_health_ms(row["elapsed_ms"]) if row["done"] else ""
                    ),
                }
                for row in self._health_rows.values()
            ]

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop background work and close the task manager."""
        self._music_stop.set()
        # A window closed immediately after launch may still be restoring the
        # queue.  Let that short import/read finish before closing the engine;
        # a wedged restore must not hold the close for long.
        thread = self._services_thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)

        async def close_manager() -> None:
            from novel_downloader.apps.core.task_manager import manager

            # Flushes the queue one last time; a no-op when persistence was
            # never enabled for this process.
            await manager.close()

        try:
            self._engine.submit(close_manager()).result(5)
        except Exception:  # noqa: BLE001 - never block the window closing
            logger.debug("Task manager shutdown failed", exc_info=True)
        self._engine.shutdown()


# ---------------------------------------------------------------------------
# window entry point
# ---------------------------------------------------------------------------


def run_webview_app() -> int:
    """
    Create the pywebview window and run until it closes.

    :return: Process exit code (``0`` after the window closes).
    """
    import webview

    from novel_downloader.apps.desktop.window_state import (
        clamp_geometry,
        load_window_geometry,
        window_state_file,
    )

    # Held until the process exits so the installer's Restart Manager can see
    # (and close) this instance deterministically.
    _hold_update_mutex()

    bridge = ApiBridge()
    # Importing the task manager and reading tasks.json used to sit between
    # the user's click and the first paint; it now overlaps window creation.
    bridge.start_services_background()
    geometry = load_window_geometry(window_state_file())
    if geometry is not None:
        # Fit the saved geometry to the *current* monitors: a window saved on
        # a larger screen must not reopen with its right edge (and the
        # primary buttons) clipped off-screen.
        geometry = clamp_geometry(geometry, _virtual_screen_rect())
    kwargs: dict[str, Any] = {
        "width": geometry.width if geometry else 1220,
        "height": geometry.height if geometry else 800,
        "min_size": (980, 640),
    }
    if geometry is not None and geometry.x is not None and geometry.y is not None:
        # Negative origins address monitors left of/above the primary; the
        # clamp above already kept them inside the virtual desktop.
        kwargs["x"], kwargs["y"] = geometry.x, geometry.y

    window = webview.create_window(
        "书音 BookTone",
        html=APP_HTML,
        js_api=bridge,
        background_color="#f8fafc",
        **kwargs,
    )
    assert window is not None  # nb: pywebview types this as Optional[Window]
    bridge._attach_window(window)

    def on_closing() -> None:
        # Geometry must be read on ``closing``: by the ``closed`` event the
        # GUI backend is already torn down and ``window.width`` raises.
        _save_window_state(window)

    def on_closed() -> None:
        bridge.shutdown()

    window.events.closing += on_closing
    window.events.closed += on_closed
    webview.start()
    return 0


def _save_window_state(window: Any) -> None:
    """Persist the closing window geometry in the format ``load`` accepts."""
    from novel_downloader.apps.desktop.window_state import (
        clamp_geometry,
        save_window_geometry,
        window_geometry_from_mapping,
        window_state_file,
    )

    try:
        geometry = window_geometry_from_mapping(
            {
                "width": int(window.width),
                "height": int(window.height),
                "x": int(window.x) if window.x is not None else None,
                "y": int(window.y) if window.y is not None else None,
            }
        )
    except Exception:  # noqa: BLE001 - backend may be going away mid-read
        logger.debug("Could not read the window geometry on close", exc_info=True)
        return
    if geometry is None:
        return
    try:
        save_window_geometry(window_state_file(), clamp_geometry(geometry))
    except OSError:
        logger.debug("Could not persist window state", exc_info=True)
