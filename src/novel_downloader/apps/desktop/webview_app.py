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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Parse ``"1.2.3"`` (optionally with a non-numeric suffix) for compares."""
    head = str(raw or "").strip().split("-")[0].lstrip("vV")
    parts: list[int] = []
    for chunk in head.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


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
    except (OSError, ValueError):
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
    except OSError:
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

    # ------------------------------------------------------------------
    # system
    # ------------------------------------------------------------------

    def get_initial_state(self) -> dict[str, Any]:
        """One-shot bootstrap payload for the front end."""
        prefs = _load_prefs()
        return {
            "version": self._version(),
            "theme": str(prefs.get("theme") or "light"),
            "data_dir": str(Path.cwd()),
            "is_frozen": bool(getattr(sys, "frozen", False)),
            "shortcut_exists": self.shortcut_exists(),
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

        :return: ``{"available", "latest", "url", "message"?}``.
        """
        prefs = _load_prefs()
        last = float(prefs.get("last_update_check") or 0.0)
        now = time.time()
        if now - last < _UPDATE_CHECK_INTERVAL:
            return {"available": False, "throttled": True}
        _save_prefs({"last_update_check": str(now)})

        current = self._version()
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
        except Exception as exc:  # noqa: BLE001 - offline is normal
            logger.debug("Update check failed: %s", exc)
            return {"available": False, "error": format_error(exc)}

        latest = str(payload.get("tag_name") or "").lstrip("vV")
        url = str(payload.get("html_url") or _RELEASES_PAGE)
        if not latest or not _version_tuple(latest) > _version_tuple(current):
            return {"available": False, "latest": latest, "url": url}
        return {
            "available": True,
            "current": current,
            "latest": latest,
            "url": url,
            "message": t("Update available: v{ver}").replace("{ver}", latest),
        }

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
        per_site_limit: int = 4,
    ) -> dict[str, Any]:
        """
        Run an aggregate search and return ranked results.

        :param keyword: The search term.
        :param sites_csv: Optional comma-separated site filter.
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
        ok_count = skip_count = fail_count = 0
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
                nonlocal ok_count, skip_count, fail_count
                state = str(getattr(result, "status", "") or "").lower()
                if state == "ok":
                    ok_count += 1
                elif state == "skip":
                    skip_count += 1
                else:
                    fail_count += 1
                with self._lock:
                    self._music_done += 1

            def on_tick(fraction: float, done: int) -> None:
                with self._lock:
                    self._music_fraction = float(fraction)
                    self._music_done = int(done)

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
            fail_count += 1
            with self._lock:
                self._music_failed = True
                self._music_summary = format_error(exc)
        finally:
            with self._lock:
                self._music_active = False
                self._music_fraction = 1.0
                if not self._music_summary:
                    if fail_count == 0 and skip_count == 0:
                        self._music_summary = t("Downloaded {n} tracks").replace(
                            "{n}", str(ok_count)
                        )
                    else:
                        self._music_summary = (
                            t("Done: {ok} downloaded, {skip} skipped, {fail} failed")
                            .replace("{ok}", str(ok_count))
                            .replace("{skip}", str(skip_count))
                            .replace("{fail}", str(fail_count))
                        )
                self._music_failed = self._music_failed or fail_count > 0

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
        window_state_file,
    )

    bridge = ApiBridge()
    geometry = load_window_geometry(window_state_file())
    kwargs: dict[str, Any] = {
        "width": geometry.width if geometry else 1220,
        "height": geometry.height if geometry else 800,
        "min_size": (980, 640),
    }
    if geometry is not None and geometry.x is not None and geometry.y is not None:
        kwargs["x"] = max(geometry.x, 0)
        kwargs["y"] = max(geometry.y, 0)

    window = webview.create_window(
        "Novel Downloader",
        html=APP_HTML,
        js_api=bridge,
        background_color="#f6f5f1",
        **kwargs,
    )
    assert window is not None  # nb: pywebview types this as Optional[Window]

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
