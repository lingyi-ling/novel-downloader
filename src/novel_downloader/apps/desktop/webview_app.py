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

import dataclasses
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.apps.common.updater import (
    DEFAULT_REPO,
    DEFAULT_TIMEOUT,
    UpdateInfo,
    check_for_update,
    detect_platform,
    download_asset,
    parse_manifest,
    parse_version,
    verify_sha256,
)
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES, SEARCH_SUPPORT_SITES
from novel_downloader.apps.desktop.engine import AsyncEngine
from novel_downloader.apps.desktop.formatting import format_error, status_label
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.infra.i18n import t

__all__ = ["ApiBridge", "run_webview_app"]

logger = logging.getLogger(__name__)

#: Per-site search timeout handed to :func:`plugins.search.search`.
SEARCH_SITE_TIMEOUT = 6.0
#: Upper bound for awaiting a submitted search on the engine loop.
SEARCH_WAIT_TIMEOUT = 180.0
#: Simultaneous probes while checking every music platform.
HEALTH_WORKERS = 8
#: Upper bound for awaiting task-manager coroutines.
TASK_WAIT_TIMEOUT = 30.0

_PREFS_FILENAME = "desktop_prefs.json"

#: Name of the shortcut files created on the desktop and in the Start menu.
_SHORTCUT_NAME = "Novel Downloader.lnk"

#: GitHub endpoints used by the in-app update check.
_RELEASES_API = (
    "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/latest"
)
_RELEASES_PAGE = "https://github.com/lingyi-ling/novel-downloader/releases/latest"
#: Minimum interval between update checks (24 hours).
_UPDATE_CHECK_INTERVAL = 24 * 3600.0
#: Network timeout for an update check (kept at the shared default, 8 s).
_UPDATE_TIMEOUT = DEFAULT_TIMEOUT
#: Directory under :func:`app_home` holding downloaded update files.
_UPDATES_DIRNAME = "updates"
#: Timeout for an asset transfer, in seconds.  Downloads are much larger than
#: the release document, so they get their own (generous) budget.
_UPDATE_DOWNLOAD_TIMEOUT = 300.0
#: Named mutex mirrored by ``AppMutex`` in ``packaging/noveldl_installer.iss``:
#: holding it lets the installer's Restart Manager identify this process.
_UPDATE_MUTEX_NAME = "NovelDownloader-UpdateMutex"
#: Arguments for the Inno Setup installer: no wizard, no message boxes, and a
#: Restart-Manager request to close (then restart) a running instance.
_INSTALLER_ARGS = (
    "/SILENT",
    "/SUPPRESSMSGBOXES",
    "/CLOSEAPPLICATIONS",
    "/RESTARTAPPLICATIONS",
)
#: Delay before closing the window after the installer starts.  Only long
#: enough for the ``install_update`` reply to cross the JS bridge; the process
#: never waits for the installer itself.
_INSTALL_EXIT_DELAY = 0.25
#: Grace period before retrying a window close that raised, so a GUI thread
#: still delivering the bridge reply cannot make us give up too early.
_INSTALL_EXIT_RETRY_DELAY = 1.0

# ``_version_tuple`` is the pre-existing name used by the bridge tests; the
# implementation lives in :mod:`apps.common.updater` so the update logic has a
# single source of truth (and the common module stays GUI-free).
_version_tuple = parse_version


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
  $lnk.Description = 'Novel Downloader'
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
            from novel_downloader.apps.music.downloader import download_many
            from novel_downloader.apps.music.providers import (
                available_labels,
                create,
                health_check,
                platform_note,
            )
            from novel_downloader.apps.music.providers.base import Track
        except Exception as exc:  # noqa: BLE001 - optional extras missing
            logger.debug("Music extras unavailable: %s", exc)
            _MUSIC_FUNCS = {}
            return None
        _MUSIC_FUNCS = {
            "available_labels": available_labels,
            "create": create,
            "health_check": health_check,
            "platform_note": platform_note,
            "download_many": download_many,
            "Track": Track,
        }
    return _MUSIC_FUNCS or None


def _music_options_type(**kwargs: Any) -> Any:
    """Construct :class:`DownloadOptions` from the (untyped) music extras."""
    from novel_downloader.apps.music.downloader import DownloadOptions

    return DownloadOptions(**kwargs)


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
        "Search": t("Search"),
        "Download": t("Download"),
        "History": t("History"),
        "Music": t("Music"),
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
        "Ctrl+1-5 switch pages": t("Ctrl+1-5 switch pages"),
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
        "Search failed": t("Search failed"),
        "No results": t("No results"),
        "Try another keyword or clear the site filter": t(
            "Try another keyword or clear the site filter"
        ),
        "Author": t("Author"),
        "Best match": t("Best match"),
        "Open in browser": t("Open in browser"),
        "Queued: {title}": t("Queued: {title}"),
        # download page
        "Active": t("Active"),
        "Nothing in the queue": t("Nothing in the queue"),
        "Search for a book and press Download": t(
            "Search for a book and press Download"
        ),
        "No tasks yet": t("No tasks yet"),
        # history page
        "{n} files in {dir}": t("{n} files in {dir}"),
        "No exported books yet": t("No exported books yet"),
        "Files land here when a download finishes": t(
            "Files land here when a download finishes"
        ),
        # music page
        "Search a song to begin": t("Search a song to begin"),
        "A download is already running": t("A download is already running"),
        "Music extras are not installed": t("Music extras are not installed"),
        "Downloaded {n} tracks": t("Downloaded {n} tracks"),
        "Done: {ok} downloaded, {skip} skipped, {fail} failed": t(
            "Done: {ok} downloaded, {skip} skipped, {fail} failed"
        ),
        # follow page
        "Added to follow list": t("Added to follow list"),
        "Already followed": t("Already followed"),
        "No chapter recorded yet": t("No chapter recorded yet"),
        "No followed books": t("No followed books"),
        "Follow a search result or add a book manually": t(
            "Follow a search result or add a book manually"
        ),
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


def _music_track_dict(track: object) -> dict[str, Any]:
    """Serialise a music provider track dataclass."""
    try:
        return dict(dataclasses.asdict(track))  # type: ignore[call-overload]
    except Exception:  # noqa: BLE001 - fall back to attribute copy
        return {
            "name": str(getattr(track, "name", "") or ""),
            "artists": str(getattr(track, "artists", "") or ""),
            "album": str(getattr(track, "album", "") or ""),
            "duration_s": int(getattr(track, "duration_s", 0) or 0),
            "vip": bool(getattr(track, "vip", False)),
            "ext": str(getattr(track, "ext", "") or ""),
        }


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
    """A downloaded asset whose checksum was verified by us."""

    path: Path
    sha256: str
    name: str


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
        return detect_platform()
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


def _save_prefs(values: dict[str, Any]) -> None:
    """Merge ``values`` into the preference file (best effort)."""
    path = _prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = _load_prefs()
        stored.update(values)
        path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, TypeError, ValueError):
        # ``TypeError``/``ValueError`` guard against unserialisable stored
        # content; preferences are best effort and never fatal.
        logger.debug("Could not persist desktop prefs", exc_info=True)


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
        self._engine = AsyncEngine()
        self._lock = threading.Lock()
        # music download state (polled by the front end)
        self._music_active = False
        self._music_done = 0
        self._music_total = 0
        self._music_fraction = 0.0
        self._music_stop = threading.Event()
        self._music_summary = ""
        self._music_failed = False
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
        self._exit_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # system
    # ------------------------------------------------------------------

    def get_initial_state(self) -> dict[str, Any]:
        """One-shot bootstrap payload for the front end."""
        prefs = _load_prefs()
        # Only the two known themes are handed to the page: a hand-edited
        # preference file must not inject an arbitrary string into the front
        # end (the JS side re-normalises, this keeps the stored value sane).
        theme = str(prefs.get("theme") or "light")
        if theme not in ("light", "dark"):
            theme = "light"
        return {
            "version": self._version(),
            "theme": theme,
            "data_dir": str(Path.cwd()),
            "is_frozen": bool(getattr(sys, "frozen", False)),
            "shortcut_exists": self.shortcut_exists(),
            "auto_check_updates": _prefs_flag(prefs, "auto_check_updates", True),
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
        """Current working directory (the application home)."""
        return str(Path.cwd())

    def save_prefs(self, values: dict[str, Any]) -> None:
        """Persist front-end preferences such as the theme."""
        if isinstance(values, dict):
            _save_prefs(values)

    def open_path(self, path: str) -> None:
        """Open a file or directory with the OS default handler."""
        target = str(path or "").strip()
        if not target or not Path(target).exists():
            return
        try:
            if sys.platform == "win32":
                startfile = getattr(os, "startfile", None)
                if startfile is not None:
                    startfile(target)  # noqa: S606 - user-initiated shell open
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])  # noqa: S603
            else:
                subprocess.Popen(["xdg-open", target])  # noqa: S603
        except OSError:
            logger.exception("Failed to open %s", target)

    def open_url(self, url: str) -> None:
        """Open an external URL in the default browser."""
        target = str(url or "").strip()
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)

    def open_downloads_dir(self) -> None:
        """Open the export directory in the file manager."""
        try:
            from novel_downloader.infra.config import get_config_value

            out_dir = Path(get_config_value(["general", "output_dir"], "./downloads"))
        except Exception:  # noqa: BLE001 - config may be missing on first run
            out_dir = Path("./downloads")
        out_dir = out_dir.expanduser()
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        self.open_path(str(out_dir.resolve()))

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

    def _attach_window(self, window: Any) -> None:
        """
        Register the pywebview window so an update can close the app.

        :param window: The object returned by ``webview.create_window``.
        """

        def request_exit() -> None:
            logger.info("Closing the window so the installer can replace files")
            window.destroy()

        self._exit_callback = request_exit

    def _remember_check(
        self,
        info: UpdateInfo | None,
        *,
        current: str,
        platform: str,
        checked_at: float,
        error: str | None,
    ) -> None:
        """Store one interpreted check for :meth:`update_state` to serve."""
        with self._update_guard:
            self._update_cache = _UpdateCache(
                info=info,
                error=error,
                checked_at=checked_at,
                current=current,
                platform=platform,
            )

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
            "asset_name": None,
            "asset_size": None,
            "platform": "",
            "checked_at": 0.0,
            "error": error,
            "pending": False,
            "auto_check": auto,
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
        return {
            "current": cache.current if cache is not None else self._version(),
            "latest": info.latest if info is not None else "",
            "available": bool(info is not None and info.available),
            "notes_url": (
                info.notes_url
                if info is not None and info.notes_url
                else _RELEASES_PAGE
            ),
            "asset_name": asset.name if asset is not None else None,
            "asset_size": asset.size if asset is not None else None,
            "platform": (cache.platform if cache is not None else _safe_platform_tag()),
            "checked_at": float(cache.checked_at) if cache is not None else 0.0,
            "error": cache.error if cache is not None else None,
            "pending": inflight,
            "auto_check": _prefs_flag(stored, "auto_check_updates", True),
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
                slug=DEFAULT_REPO,
                platform_tag=platform_tag,
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

    def _download_update(self) -> dict[str, Any]:
        """Implementation of :meth:`download_update` (may raise)."""
        with self._download_guard:
            if self._download_active:
                return self._download_error(t("An update download is already running"))
            self._download_active = True
        try:
            with self._update_guard:
                cache = self._update_cache
            info = cache.info if cache is not None else None
            if info is None or not info.available:
                return self._download_error(t("No update is available to download"))
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
            name = Path(str(asset.name).replace("\\", "/")).name or "update.bin"
            try:
                written = download_asset(
                    asset.url,
                    Path(name),
                    expected_sha256=digest,
                    root=updates_dir,
                    timeout=_UPDATE_DOWNLOAD_TIMEOUT,
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
                    path=final, sha256=digest, name=final.name
                )
            return {
                "ok": True,
                "path": str(final),
                "error": None,
                "asset_name": final.name,
            }
        finally:
            with self._download_guard:
                self._download_active = False

    @staticmethod
    def _download_error(message: str) -> dict[str, Any]:
        """A failed :meth:`download_update` reply."""
        return {"ok": False, "path": None, "error": message}

    def _install_update(self) -> dict[str, Any]:
        """Implementation of :meth:`install_update` (may raise)."""
        # Claim the verified record atomically.  The page disables its buttons
        # while installing, but any script in the window can call the bridge
        # twice; without the claim both calls would reach ``Popen`` and start
        # two installers.  Every failure that leaves the file usable gives the
        # claim back (see :meth:`_release_claim`).
        with self._download_guard:
            record = self._downloaded
            self._downloaded = None
        if record is None:
            return {"ok": False, "error": t("No update has been downloaded yet")}
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
        if target.suffix.lower() != ".exe":
            self._release_claim(record)
            return {
                "ok": False,
                "error": t(
                    "This update is not an installer; please extract it manually"
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

        try:
            # Fixed argument vector: no shell, no user-supplied text.  A list
            # argument keeps an installer path containing spaces intact
            # (``list2cmdline`` quotes it).
            process = subprocess.Popen(  # noqa: S603
                [str(target), *_INSTALLER_ARGS],
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
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

    def search(
        self,
        keyword: str,
        sites_csv: str = "",
        group: bool = False,
        per_site_limit: int = 4,
    ) -> dict[str, Any]:
        """
        Run an aggregate search and return ranked results.

        :param keyword: The search term.
        :param sites_csv: Optional comma-separated site filter.
        :param group: Merge same-book results into one entry per book, each
            carrying the ``sources`` capsules (best source first).
        :param per_site_limit: Results kept per site before ranking.
        """
        text = str(keyword or "").strip()
        if not text:
            return {"results": [], "elapsed": 0.0}
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

    def check_updates(self) -> dict[str, Any]:
        """Check every followed book for a new chapter (blocking)."""

        async def run() -> list[Any]:
            from novel_downloader.apps.core.watchlist import WatchlistStore, check_all

            return list(await check_all(WatchlistStore()))

        try:
            results = self._engine.submit(run()).result(300.0)
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Follow check failed")
            return {"checked": 0, "updated": 0, "failed": 0, "error": format_error(exc)}
        checked = len(results)
        updated = sum(1 for r in results if getattr(r, "changed", False))
        failed = sum(1 for r in results if getattr(r, "error", None))
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

    def music_note(self, platform: str) -> str:
        """Usage hint for one platform (may be empty)."""
        funcs = _music_funcs()
        if funcs is None:
            return ""
        return str(funcs["platform_note"](str(platform or "")) or "")

    def music_search(
        self, platform: str, keyword: str, limit: int = 30
    ) -> dict[str, Any]:
        """Search one platform and return serialised tracks."""
        funcs = _music_funcs()
        if funcs is None:
            return {"tracks": [], "error": t("Music extras are not installed")}
        try:
            text = str(keyword or "").strip()
            if not text:
                return {"tracks": []}
            provider = funcs["create"](str(platform or ""))
            tracks = provider.search(text, limit=max(1, int(limit)))
        except Exception as exc:  # noqa: BLE001 - reported to the page
            logger.exception("Music search failed on %s", platform)
            return {"tracks": [], "error": format_error(exc)}
        return {"tracks": [_music_track_dict(track) for track in tracks]}

    def music_download(
        self, platform: str, tracks: list[dict[str, Any]], level: str | None = None
    ) -> dict[str, Any]:
        """
        Start downloading the selected tracks on a background thread.

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
            self._music_stop.clear()

        worker = threading.Thread(
            target=self._music_worker,
            args=(str(platform or ""), list(tracks or []), level),
            name="novel-desktop-music",
            daemon=True,
        )
        worker.start()
        return {"ok": True}

    def _music_worker(
        self, platform: str, tracks: list[dict[str, Any]], level: str | None
    ) -> None:
        """Download tracks on a worker thread, updating polled state."""
        # ``download_many`` invokes the callbacks from its own worker pool, so
        # every counter is updated under the bridge lock: an unlocked ``+=``
        # loses results whenever two tracks finish concurrently.
        counts = {"ok": 0, "skip": 0, "fail": 0}
        try:
            funcs = _music_funcs()
            if funcs is None:
                raise RuntimeError(t("Music extras are not installed"))
            provider = funcs["create"](platform)
            options = self._music_options(level)
            track_cls = funcs["Track"]
            fields = {f.name for f in dataclasses.fields(track_cls)}
            objects = [
                track_cls(**{k: v for k, v in raw.items() if k in fields})
                for raw in tracks
                if isinstance(raw, dict)
            ]
            with self._lock:
                self._music_total = len(objects)

            def on_result(_index: int, _track: object, result: object) -> None:
                state = str(getattr(result, "status", "") or "").lower()
                key = "ok" if state == "ok" else "skip" if state == "skip" else "fail"
                with self._lock:
                    counts[key] += 1
                    # Exactly one increment per finished track: the count is
                    # owned here, ``on_tick`` only drives the fraction.
                    self._music_done += 1

            def on_tick(fraction: float, _done: int) -> None:
                with self._lock:
                    # Worker threads report snapshots that can arrive out of
                    # order; keep the most advanced one so the progress bar
                    # never moves backwards.
                    self._music_fraction = max(self._music_fraction, float(fraction))

            funcs["download_many"](
                provider,
                objects,
                options,
                workers=3,
                on_result=on_result,
                on_tick=on_tick,
                should_stop=self._music_stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - reported through the poll
            logger.exception("Music download crashed")
            with self._lock:
                counts["fail"] += 1
                self._music_failed = True
                self._music_summary = format_error(exc)
        finally:
            with self._lock:
                self._music_active = False
                self._music_fraction = 1.0
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
            }

    def music_stop(self) -> dict[str, Any]:
        """Ask the running music download to stop."""
        self._music_stop.set()
        return {"ok": True}

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
        """Probe every platform once, in parallel, for the health panel."""
        funcs = _music_funcs()
        if funcs is None:
            return {"ok": False}
        with self._lock:
            if self._health_thread is not None and self._health_thread.is_alive():
                return {"ok": False}
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
                            result = funcs["health_check"](key)
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
        load_window_geometry,
        visible_origin,
        window_state_file,
    )

    # Held until the process exits so the installer's Restart Manager can see
    # (and close) this instance deterministically.
    _hold_update_mutex()

    bridge = ApiBridge()
    geometry = load_window_geometry(window_state_file())
    kwargs: dict[str, Any] = {
        "width": geometry.width if geometry else 1220,
        "height": geometry.height if geometry else 800,
        "min_size": (980, 640),
    }
    if geometry is not None and geometry.x is not None and geometry.y is not None:
        # Negative origins address monitors left of/above the primary, so the
        # position is restored as stored; it is only dropped when it no longer
        # lands on any connected monitor (the saved one was unplugged).
        origin = visible_origin(geometry.x, geometry.y, _virtual_screen_rect())
        if origin is not None:
            kwargs["x"], kwargs["y"] = origin

    window = webview.create_window(
        "Novel Downloader",
        html=APP_HTML,
        js_api=bridge,
        background_color="#f6f5f1",
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
