#!/usr/bin/env python3
"""
novel_downloader.apps.web.pages.music
--------------------------------------

Music download page: multi-platform search, NetEase login and playlists, and
a background download queue.

The music engine lives in :mod:`novel_downloader.apps.music` and depends on
the optional ``music`` extras.  It is imported inside a guard so a missing
extra turns this page into an install hint instead of breaking the whole app.
Importing the engine performs no network requests.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui
from nicegui.events import ValueChangeEventArguments

from novel_downloader.infra.i18n import t

from ..components import page_frame
from ..components.widgets import empty_state, page_header, skeleton_card

# ---------------------------------------------------------------------------
# Optional engine imports
# ---------------------------------------------------------------------------

_MUSIC_AVAILABLE = False
_MUSIC_IMPORT_ERROR = ""

try:
    # mutagen is imported lazily by the downloader; probe it here so a missing
    # extra is reported on the page rather than in the middle of a download.
    import mutagen  # noqa: F401
    import qrcode  # noqa: F401
    import websocket  # noqa: F401

    from novel_downloader.apps.music import paths, weapi
    from novel_downloader.apps.music import settings as music_settings
    from novel_downloader.apps.music.browser_login import (
        BrowserLogin,
        to_cookie_string,
    )
    from novel_downloader.apps.music.downloader import (
        DownloadOptions,
        download_many,
        human,
    )
    from novel_downloader.apps.music.providers import (
        available_labels,
        create,
        platform_note,
    )
    from novel_downloader.apps.music.providers.base import Track

    _MUSIC_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - optional dependency guard
    _MUSIC_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_QUALITY_KEYS = ("hires", "lossless", "exhigh", "higher", "standard")

_DEFAULT_WORKERS = 3
_MAX_WORKERS = 8


@dataclass
class _State:
    """Per-connection state for the music page."""

    provider_key: str
    tracks: list[Track] = field(default_factory=list)
    selected: set[int] = field(default_factory=set)
    list_title: str = ""
    loading: bool = False
    playlists: list[dict[str, Any]] = field(default_factory=list)
    playlists_loading: bool = False
    logged_in: bool = False
    account_name: str = ""
    login_running: bool = False
    login_status: str = ""
    downloading: bool = False
    stop_requested: bool = False
    stopped: bool = False
    download_total: int = 0
    providers: dict[str, Any] = field(default_factory=dict)
    events: queue.Queue[tuple[str, Any]] = field(default_factory=queue.Queue)
    list_seq: int = 0
    download_seq: int = 0
    login_seq: int = 0


def _spawn(target: Callable[[], None]) -> None:
    """Run ``target`` in a daemon thread; it must never touch the UI."""
    threading.Thread(target=target, daemon=True).start()


def _format_duration(seconds: int) -> str:
    """Format a track duration as ``m:ss`` or ``h:mm:ss``."""
    if seconds <= 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _render_missing_deps() -> None:
    """Render the friendly hint shown when the music extras are absent."""
    with ui.column().classes("nd-page w-full max-w-screen-lg min-w-[320px] mx-auto"):
        page_header(
            t("Music"),
            t("Download songs from multiple platforms"),
        )
        with ui.card().props("flat").classes("nd-toolbar w-full"):
            empty_state(
                "library_music",
                t(
                    "Install the music extras to enable this page: "
                    'pip install "novel-downloader[music]"'
                ),
            )
            if _MUSIC_IMPORT_ERROR:
                ui.label(
                    t("Import error: {error}").format(error=_MUSIC_IMPORT_ERROR)
                ).classes("text-xs nd-muted")


@ui.page("/music")
def page_music() -> None:
    """Render the music download page."""
    ui.page_title(f"{t('Music')} · {t('Novel Downloader')}")
    page_frame("music")

    if not _MUSIC_AVAILABLE:
        _render_missing_deps()
        return

    try:
        labels = available_labels()  # type: ignore[no-untyped-call]
    except Exception as exc:  # noqa: BLE001 - provider registry failure
        _render_missing_deps()
        ui.notify(
            t("Could not load music platforms: {error}").format(error=str(exc)),
            type="negative",
        )
        return

    label_map = dict(labels)
    if not labels:
        _render_missing_deps()
        return

    saved = music_settings.load_settings()
    default_key = "netease" if "netease" in label_map else labels[0][0]
    state = _State(provider_key=default_key)

    def _provider_for(key: str) -> Any:
        provider = state.providers.get(key)
        if provider is None:
            provider = create(key)  # type: ignore[no-untyped-call]
            state.providers[key] = provider
        return provider

    def _netease() -> Any | None:
        try:
            return _provider_for("netease")
        except Exception as exc:  # noqa: BLE001 - provider construction
            ui.notify(
                t("Could not start the NetEase provider: {error}").format(
                    error=str(exc)
                ),
                type="negative",
            )
            return None

    # Probe the persisted NetEase session immediately; is_logged_in() only
    # inspects cookies and performs no network request.
    if state.provider_key == "netease":
        probe = _netease()
        if probe is not None:
            state.logged_in = bool(probe.is_logged_in())

    def _workers_value() -> int:
        try:
            value = int(round(float(workers_in.value)))
        except (TypeError, ValueError):
            value = _DEFAULT_WORKERS
        return max(1, min(_MAX_WORKERS, value))

    def _save_current_settings() -> None:
        music_settings.save_settings(
            {
                "out_dir": str(out_dir_in.value or "").strip(),
                "level": str(level_sel.value or "exhigh"),
                "embed_tags": bool(tags_cb.value),
                "embed_cover": bool(cover_cb.value),
                "save_lyric": bool(lyric_cb.value),
                "split_folder": bool(split_cb.value),
                "skip_existing": bool(skip_cb.value),
                "workers": _workers_value(),
            }
        )

    def _on_setting_change(_: ValueChangeEventArguments[Any]) -> None:
        _save_current_settings()

    # ------------------------------------------------------------------
    # Download helpers
    # ------------------------------------------------------------------

    def _append_log(status: str, text: str, detail: str) -> None:
        if status == "ok":
            label, pill = t("OK"), "nd-pill--completed"
        elif status == "skip":
            label, pill = t("Skipped"), ""
        else:
            label, pill = t("Failed"), "nd-pill--failed"
        with log_area, ui.row().classes("items-start gap-2 w-full no-wrap"):
            ui.label(label).classes(f"nd-pill {pill} shrink-0")
            with ui.column().classes("min-w-0 gap-0"):
                ui.label(text).classes("text-xs break-words")
                if detail:
                    ui.label(detail).classes("text-xs nd-muted break-words")

    def _finish_download(ok: int, skip: int, fail: int) -> None:
        state.downloading = False
        state.stop_requested = False
        done = ok + skip + fail
        total = state.download_total or 1
        progress_bar.set_value(min(1.0, done / total))
        summary_label.set_text(
            t("Done: {ok} succeeded, {skip} skipped, {fail} failed").format(
                ok=ok, skip=skip, fail=fail
            )
        )
        dl_btn.enable()
        stop_btn.disable()

    def _finish_download_error(message: str) -> None:
        state.downloading = False
        state.stop_requested = False
        summary_label.set_text(t("Download stopped: {error}").format(error=message))
        ui.notify(t("Download failed: {error}").format(error=message), type="negative")
        dl_btn.enable()
        stop_btn.disable()

    def _on_session_expired() -> None:
        state.logged_in = False
        state.account_name = ""
        state.playlists = []
        state.playlists_loading = False
        state.login_status = ""
        render_login.refresh()
        render_playlists.refresh()
        ui.notify(t("Session expired, please log in again."), type="warning")

    def _start_download(tracks: list[Any]) -> None:
        if state.downloading:
            ui.notify(t("A download is already running"), type="warning")
            return
        if not tracks:
            ui.notify(t("Select at least one song first"), type="warning")
            return
        try:
            provider = _provider_for(state.provider_key)
        except Exception as exc:  # noqa: BLE001 - provider construction
            ui.notify(
                t("Could not start this platform: {error}").format(error=str(exc)),
                type="negative",
            )
            return

        opts = DownloadOptions(
            out_dir=str(out_dir_in.value or "").strip()
            or str(paths.default_download_dir()),
            level=str(level_sel.value or "exhigh"),
            embed_tags=bool(tags_cb.value),
            embed_cover=bool(cover_cb.value),
            save_lyric=bool(lyric_cb.value),
            split_folder=bool(split_cb.value),
            skip_existing=bool(skip_cb.value),
            retries=2,
        )
        workers = _workers_value()

        state.download_seq += 1
        seq = state.download_seq
        state.downloading = True
        state.stop_requested = False
        state.download_total = len(tracks)
        log_area.clear()
        progress_bar.set_value(0)
        progress_label.set_text(
            t("Completed {done} of {total}").format(done=0, total=len(tracks))
        )
        summary_label.set_text(t("Downloading..."))
        dl_btn.disable()
        stop_btn.enable()
        events = state.events

        def on_result(index: int, track: Any, result: Any) -> None:
            status = str(getattr(result, "status", "fail"))
            reason = str(getattr(result, "reason", "") or "")
            try:
                size = int(getattr(result, "bytes_", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            events.put(
                (
                    "result",
                    (
                        seq,
                        str(getattr(track, "name", "")),
                        str(getattr(track, "artists", "")),
                        status,
                        reason,
                        human(size)  # type: ignore[no-untyped-call]
                        if status == "ok"
                        else "",
                    ),
                )
            )

        def on_tick(fraction: float, done: int) -> None:
            events.put(("tick", (seq, fraction, done)))

        def should_stop() -> bool:
            return state.stop_requested or state.stopped

        def work() -> None:
            try:
                ok, skip, fail, _pairs, _failed = download_many(  # type: ignore[no-untyped-call]
                    provider,
                    tracks,
                    opts,
                    workers=workers,
                    on_result=on_result,
                    on_tick=on_tick,
                    should_stop=should_stop,
                )
            except weapi.LoginExpired:
                events.put(("dl_expired", seq))
            except Exception as exc:  # noqa: BLE001 - background worker
                events.put(("dl_error", (seq, f"{type(exc).__name__}: {exc}")))
            else:
                events.put(("done", (seq, ok, skip, fail)))

        _spawn(work)

    def _download_selected() -> None:
        selected = [
            state.tracks[index]
            for index in sorted(state.selected)
            if 0 <= index < len(state.tracks)
        ]
        _start_download(selected)

    def _stop_download() -> None:
        if not state.downloading:
            return
        state.stop_requested = True
        summary_label.set_text(t("Stopping..."))
        stop_btn.disable()

    def _row_download_handler(track: Any) -> Callable[[], None]:
        def handler() -> None:
            _start_download([track])

        return handler

    # ------------------------------------------------------------------
    # Search / playlist loading
    # ------------------------------------------------------------------

    def _do_search() -> None:
        keyword = str(search_in.value or "").strip()
        if not keyword:
            ui.notify(t("Please enter a keyword"), type="warning")
            return
        if state.loading:
            return
        try:
            provider = _provider_for(state.provider_key)
        except Exception as exc:  # noqa: BLE001 - provider construction
            ui.notify(
                t("Could not start this platform: {error}").format(error=str(exc)),
                type="negative",
            )
            return

        state.list_seq += 1
        seq = state.list_seq
        state.loading = True
        state.list_title = t("Search results")
        state.selected.clear()
        search_btn.props("loading")
        render_tracks.refresh()
        events = state.events

        def work() -> None:
            try:
                tracks, total = provider.search(keyword, limit=30, offset=0)
            except weapi.LoginExpired:
                events.put(("session_expired", ""))
            except Exception as exc:  # noqa: BLE001 - provider request
                events.put(("list_fail", (seq, f"{type(exc).__name__}: {exc}")))
            else:
                events.put(("list_ok", (seq, list(tracks), total, True)))

        _spawn(work)

    def _load_playlist(pid: Any, title: str) -> None:
        if pid is None:
            return
        provider = _netease()
        if provider is None:
            return

        state.list_seq += 1
        seq = state.list_seq
        state.loading = True
        state.list_title = t("Playlist: {name}").format(name=title)
        state.selected.clear()
        render_tracks.refresh()
        events = state.events

        def work() -> None:
            try:
                tracks = provider.playlist_tracks(pid)
            except weapi.LoginExpired:
                events.put(("session_expired", ""))
            except Exception as exc:  # noqa: BLE001 - provider request
                events.put(("list_fail", (seq, f"{type(exc).__name__}: {exc}")))
            else:
                events.put(("list_ok", (seq, list(tracks), 0, False)))

        _spawn(work)

    def _refresh_netease() -> None:
        provider = _netease()
        if provider is None:
            return
        if not provider.is_logged_in():
            state.logged_in = False
            state.account_name = ""
            state.playlists = []
            state.playlists_loading = False
            render_login.refresh()
            render_playlists.refresh()
            return

        state.logged_in = True
        state.playlists_loading = True
        render_login.refresh()
        render_playlists.refresh()
        events = state.events

        def work() -> None:
            try:
                account, profile = provider.account()
            except weapi.LoginExpired:
                events.put(("session_expired", ""))
                return
            except Exception as exc:  # noqa: BLE001 - provider request
                events.put(("netease_fail", f"{type(exc).__name__}: {exc}"))
                return
            if not account or not profile:
                events.put(("session_expired", ""))
                return
            uid = profile.get("userId")
            try:
                playlists = provider.user_playlists(uid)
            except weapi.LoginExpired:
                events.put(("session_expired", ""))
                return
            except Exception as exc:  # noqa: BLE001 - provider request
                events.put(("netease_fail", f"{type(exc).__name__}: {exc}"))
                return
            events.put(("netease_ok", (profile, playlists)))

        _spawn(work)

    def _logout() -> None:
        provider = _netease()
        if provider is None:
            return
        try:
            provider.clear_cookies()
        except Exception as exc:  # noqa: BLE001 - disk/cookie cleanup
            ui.notify(
                t("Log out failed: {error}").format(error=str(exc)),
                type="negative",
            )
            return
        state.logged_in = False
        state.account_name = ""
        state.login_status = ""
        state.playlists = []
        state.playlists_loading = False
        state.tracks = []
        state.selected.clear()
        state.list_title = ""
        state.list_seq += 1
        render_login.refresh()
        render_playlists.refresh()
        render_tracks.refresh()
        ui.notify(t("Logged out"))

    def _start_login() -> None:
        if state.login_running:
            return
        provider = _netease()
        if provider is None:
            return

        state.login_seq += 1
        seq = state.login_seq
        state.login_running = True
        state.login_status = t("Opening the browser...")
        render_login.refresh()
        events = state.events

        def work() -> None:
            browser: Any = None
            try:
                browser = BrowserLogin(  # type: ignore[no-untyped-call]
                    str(paths.browser_profile_dir())
                )
                browser.start()
                browser.open_login_page()
                events.put(
                    (
                        "login_status",
                        (seq, t("Complete the sign-in in the browser window.")),
                    )
                )
                cookies = browser.wait_for_login(
                    timeout=240,
                    on_status=lambda message, _important: events.put(
                        ("login_status", (seq, str(message)))
                    ),
                    should_stop=lambda: state.stopped,
                )
                events.put(("login_status", (seq, t("Verifying the session..."))))
                provider.login_by_cookie(
                    to_cookie_string(cookies)  # type: ignore[no-untyped-call]
                )
            except Exception as exc:  # noqa: BLE001 - browser login flow
                events.put(("login_fail", (seq, f"{type(exc).__name__}: {exc}")))
            else:
                events.put(("login_ok", seq))
            finally:
                if browser is not None:
                    with suppress(Exception):
                        browser.close()

        _spawn(work)

    def _on_platform_change(e: ValueChangeEventArguments[Any]) -> None:
        state.provider_key = str(e.value or "")
        state.tracks = []
        state.selected.clear()
        state.list_title = ""
        state.loading = False
        state.playlists = []
        state.playlists_loading = False
        state.account_name = ""
        state.logged_in = False
        state.list_seq += 1
        render_note.refresh()
        render_login.refresh()
        render_playlists.refresh()
        render_tracks.refresh()
        if state.provider_key == "netease":
            provider = _netease()
            if provider is not None and provider.is_logged_in():
                state.logged_in = True
                render_login.refresh()
                _refresh_netease()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    sel_label: Any = None

    def _sync_selection() -> None:
        if sel_label is None:
            return
        with suppress(Exception):
            sel_label.set_text(t("{count} selected").format(count=len(state.selected)))

    def _toggle_selection(e: ValueChangeEventArguments[Any], index: int) -> None:
        if e.value:
            state.selected.add(index)
        else:
            state.selected.discard(index)
        _sync_selection()

    def _select_all() -> None:
        state.selected = set(range(len(state.tracks)))
        render_tracks.refresh()

    def _invert_selection() -> None:
        state.selected = set(range(len(state.tracks))) - state.selected
        render_tracks.refresh()

    # ------------------------------------------------------------------
    # Event drain (runs on the UI thread via ui.timer)
    # ------------------------------------------------------------------

    def _handle(kind: str, payload: Any) -> None:
        if kind == "list_ok":
            seq, tracks, total, is_search = payload
            if seq != state.list_seq:
                return
            state.tracks = list(tracks)
            state.selected.clear()
            state.loading = False
            if is_search:
                state.list_title = t("Search results ({shown} of {total})").format(
                    shown=len(tracks), total=max(int(total or 0), len(tracks))
                )
            search_btn.props(remove="loading")
            render_tracks.refresh()
        elif kind == "list_fail":
            seq, error = payload
            if seq != state.list_seq:
                return
            state.loading = False
            search_btn.props(remove="loading")
            render_tracks.refresh()
            ui.notify(
                t("Could not load songs: {error}").format(error=error),
                type="negative",
            )
        elif kind == "netease_ok":
            profile, playlists = payload
            if state.provider_key != "netease":
                return
            state.logged_in = True
            state.account_name = str(profile.get("nickname") or "")
            state.playlists = list(playlists)
            state.playlists_loading = False
            render_login.refresh()
            render_playlists.refresh()
        elif kind == "netease_fail":
            if state.provider_key != "netease":
                return
            state.playlists_loading = False
            render_playlists.refresh()
            ui.notify(
                t("Could not load playlists: {error}").format(error=payload),
                type="negative",
            )
        elif kind == "session_expired":
            if state.provider_key != "netease":
                return
            _on_session_expired()
        elif kind == "login_status":
            seq, message = payload
            if seq != state.login_seq:
                return
            state.login_status = str(message)
            render_login.refresh()
        elif kind == "login_ok":
            seq = payload
            if seq != state.login_seq:
                return
            state.login_running = False
            state.logged_in = True
            state.login_status = ""
            render_login.refresh()
            ui.notify(t("Login successful"), type="positive")
            _refresh_netease()
        elif kind == "login_fail":
            seq, error = payload
            if seq != state.login_seq:
                return
            state.login_running = False
            state.login_status = ""
            render_login.refresh()
            ui.notify(t("Login failed: {error}").format(error=error), type="negative")
        elif kind == "tick":
            seq, fraction, done = payload
            if seq != state.download_seq:
                return
            progress_bar.set_value(float(fraction))
            progress_label.set_text(
                t("Completed {done} of {total}").format(
                    done=done, total=state.download_total
                )
            )
        elif kind == "result":
            seq, name, artists, status, reason, size = payload
            if seq != state.download_seq:
                return
            _append_log(status, f"{name} - {artists}", reason or size)
        elif kind == "done":
            seq, ok, skip, fail = payload
            if seq != state.download_seq:
                return
            _finish_download(ok, skip, fail)
        elif kind == "dl_error":
            seq, error = payload
            if seq != state.download_seq:
                return
            _finish_download_error(error)
        elif kind == "dl_expired":
            seq = payload
            if seq != state.download_seq:
                return
            _finish_download_error(t("Session expired, please log in again."))
            _on_session_expired()

    def _drain() -> None:
        if state.stopped:
            return
        while True:
            try:
                kind, payload = state.events.get_nowait()
            except queue.Empty:
                break
            _handle(kind, payload)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    out_dir_default = str(saved.get("out_dir") or paths.default_download_dir())
    level_default = str(saved.get("level") or "exhigh")
    if level_default not in _QUALITY_KEYS:
        level_default = "exhigh"
    quality_options = {
        "hires": t("Hi-Res"),
        "lossless": t("Lossless"),
        "exhigh": t("Ex-High"),
        "higher": t("Higher"),
        "standard": t("Standard"),
    }
    try:
        workers_default = int(saved.get("workers") or _DEFAULT_WORKERS)
    except (TypeError, ValueError):
        workers_default = _DEFAULT_WORKERS
    workers_default = max(1, min(_MAX_WORKERS, workers_default))

    with ui.column().classes("nd-page w-full max-w-screen-lg min-w-[320px] mx-auto"):
        page_header(
            t("Music"),
            t("Search and download music from {count} platforms").format(
                count=len(labels)
            ),
        )

        # ---------- search toolbar ----------
        with (
            ui.card().props("flat").classes("nd-toolbar w-full"),
            ui.column().classes("w-full gap-2"),
        ):
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                platform_sel = (
                    ui.select(
                        label_map,
                        value=state.provider_key,
                        label=t("Platform"),
                        with_input=False,
                    )
                    .props("outlined dense")
                    .classes("w-[240px]")
                )
                search_in = (
                    ui.input(t("Search songs"), value="")
                    .props("outlined dense clearable autocomplete=off")
                    .classes("min-w-[220px] grow")
                )
                search_btn = ui.button(t("Search"), icon="search").props(
                    "unelevated no-caps color=primary"
                )

            note_area = ui.row().classes("items-center gap-2 w-full")
            login_area = ui.row().classes("items-center gap-2 w-full flex-wrap")

        # ---------- playlists ----------
        playlists_area = ui.column().classes("w-full gap-2")

        # ---------- track list ----------
        track_head = ui.row().classes(
            "items-center justify-between w-full gap-2 flex-wrap"
        )
        tracks_area = ui.column().classes("w-full gap-2")

        # ---------- settings ----------
        with ui.card().props("flat").classes("nd-toolbar w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("settings").classes("nd-muted")
                ui.label(t("Download settings")).classes("text-base font-medium")
            with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                out_dir_in = (
                    ui.input(t("Output directory"), value=out_dir_default)
                    .props("outlined dense")
                    .classes("grow min-w-[260px]")
                )
                level_sel = (
                    ui.select(
                        quality_options,
                        value=level_default,
                        label=t("Audio quality"),
                        with_input=False,
                    )
                    .props("outlined dense")
                    .classes("w-[180px]")
                )
            with ui.row().classes("items-center gap-3 flex-wrap"):
                tags_cb = ui.checkbox(
                    t("Embed tags"), value=bool(saved.get("embed_tags", True))
                )
                cover_cb = ui.checkbox(
                    t("Embed cover"), value=bool(saved.get("embed_cover", True))
                )
                lyric_cb = ui.checkbox(
                    t("Save lyrics"), value=bool(saved.get("save_lyric", True))
                )
                split_cb = ui.checkbox(
                    t("Split by artist/album"),
                    value=bool(saved.get("split_folder", True)),
                )
                skip_cb = ui.checkbox(
                    t("Skip existing files"),
                    value=bool(saved.get("skip_existing", True)),
                )
            with ui.row().classes("items-center gap-3"):
                workers_in = (
                    ui.number(
                        t("Concurrent downloads"),
                        value=workers_default,
                        min=1,
                        max=_MAX_WORKERS,
                        step=1,
                    )
                    .props("outlined dense")
                    .classes("w-[220px]")
                )

        # ---------- download queue ----------
        with ui.card().props("flat").classes("nd-toolbar w-full"):
            with ui.row().classes(
                "items-center justify-between w-full gap-2 flex-wrap"
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("download").classes("nd-muted")
                    ui.label(t("Download")).classes("text-base font-medium")
                with ui.row().classes("items-center gap-2"):
                    dl_btn = ui.button(
                        t("Download selected"),
                        icon="download",
                        on_click=_download_selected,
                    ).props("unelevated no-caps color=primary")
                    stop_btn = (
                        ui.button(t("Stop"), icon="stop", on_click=_stop_download)
                        .props("flat no-caps")
                        .classes("nd-btn-ghost")
                    )
                    stop_btn.disable()
            progress_bar = (
                ui.linear_progress(value=0).props("rounded").classes("w-full")
            )
            progress_label = ui.label("").classes("text-xs nd-muted")
            summary_label = ui.label("").classes("text-sm")
            with ui.scroll_area().classes("w-full max-h-[260px]"):
                log_area = ui.column().classes("w-full gap-1")

    # Settings handlers (attached after all controls exist).
    out_dir_in.on_value_change(_on_setting_change)
    level_sel.on_value_change(_on_setting_change)
    tags_cb.on_value_change(_on_setting_change)
    cover_cb.on_value_change(_on_setting_change)
    lyric_cb.on_value_change(_on_setting_change)
    split_cb.on_value_change(_on_setting_change)
    skip_cb.on_value_change(_on_setting_change)
    workers_in.on_value_change(_on_setting_change)

    # ------------------------------------------------------------------
    # Refreshables
    # ------------------------------------------------------------------

    @ui.refreshable
    def render_note() -> None:
        note_area.clear()
        note = platform_note(state.provider_key)  # type: ignore[no-untyped-call]
        if note:
            with note_area:
                ui.icon("info").classes("nd-muted")
                ui.label(note).classes("text-xs nd-muted")

    @ui.refreshable
    def render_login() -> None:
        login_area.clear()
        if state.provider_key != "netease":
            return
        with login_area:
            if state.login_running:
                ui.spinner(size="sm")
                ui.label(state.login_status or t("Waiting for login...")).classes(
                    "text-xs nd-muted"
                )
                return
            if state.logged_in:
                ui.label(state.account_name or t("Logged in")).classes(
                    "nd-pill nd-pill--completed"
                )
                ui.button(
                    t("Log out"),
                    icon="logout",
                    on_click=_logout,
                ).props("flat no-caps dense").classes("nd-btn-ghost")
            else:
                ui.label(t("Not logged in")).classes("nd-pill")
                ui.button(
                    t("Browser login"),
                    icon="login",
                    on_click=_start_login,
                ).props("flat no-caps dense").classes("nd-btn-ghost")

    def _liked_playlist() -> dict[str, Any] | None:
        for playlist in state.playlists:
            if playlist.get("specialType") == 5:
                return playlist
        for playlist in state.playlists:
            if "喜欢" in str(playlist.get("name") or ""):
                return playlist
        return None

    def _playlist_button(playlist: dict[str, Any], *, liked_entry: bool) -> None:
        pid = playlist.get("id")
        name = str(playlist.get("name") or t("Untitled playlist"))
        try:
            count = int(playlist.get("trackCount") or 0)
        except (TypeError, ValueError):
            count = 0
        label = t("Liked Songs") if liked_entry else name
        icon = "favorite" if liked_entry else "playlist_play"
        ui.button(
            label,
            icon=icon,
            on_click=lambda _=None, p=pid, n=name: _load_playlist(p, n),
        ).props("flat no-caps dense").classes("nd-pill")
        if count > 0:
            ui.label(t("{count} songs").format(count=count)).classes("text-xs nd-muted")

    @ui.refreshable
    def render_playlists() -> None:
        playlists_area.clear()
        if state.provider_key != "netease" or not state.logged_in:
            return
        liked = _liked_playlist()
        with playlists_area, ui.card().props("flat").classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("queue_music").classes("nd-muted")
                ui.label(t("My Playlists")).classes("text-base font-medium")
                if state.playlists_loading:
                    ui.spinner(size="sm")
            if liked is not None:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    _playlist_button(liked, liked_entry=True)
            others = [playlist for playlist in state.playlists if playlist is not liked]
            if others:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    for playlist in others:
                        _playlist_button(playlist, liked_entry=False)
            elif liked is None and not state.playlists_loading:
                ui.label(t("No playlists found")).classes("text-xs nd-muted")

    def _render_track_row(index: int, track: Any) -> None:
        with (
            ui.card().props("flat").classes("w-full"),
            ui.row().classes("items-center gap-3 w-full no-wrap"),
        ):
            checkbox = ui.checkbox(value=index in state.selected)
            checkbox.on_value_change(lambda e, i=index: _toggle_selection(e, i))
            with ui.column().classes("min-w-0 grow gap-0"):
                with ui.row().classes("items-center gap-2 min-w-0 no-wrap"):
                    ui.label(str(track.name)).classes("text-sm font-medium ellipsis")
                    if bool(track.vip):
                        ui.label(t("VIP")).classes("nd-pill nd-pill--running")
                ui.label(f"{track.artists} · {track.album}").classes(
                    "text-xs nd-muted ellipsis"
                )
            ui.label(_format_duration(int(track.duration_s))).classes(
                "text-xs nd-muted shrink-0"
            )
            ui.button(
                t("Download"),
                icon="download",
                on_click=_row_download_handler(track),
            ).props("flat no-caps dense").classes("nd-btn-ghost shrink-0")

    @ui.refreshable
    def render_tracks() -> None:
        nonlocal sel_label
        track_head.clear()
        tracks_area.clear()
        if state.loading:
            with tracks_area:
                skeleton_card()
                skeleton_card()
            return
        if not state.tracks:
            with tracks_area:
                empty_state(
                    "music_note",
                    t("Search or pick a playlist to load songs"),
                )
            return
        with track_head:
            ui.label(state.list_title).classes("text-sm text-caption")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.button(t("Select all"), on_click=_select_all).props(
                    "flat no-caps dense"
                ).classes("nd-btn-ghost")
                ui.button(t("Invert selection"), on_click=_invert_selection).props(
                    "flat no-caps dense"
                ).classes("nd-btn-ghost")
                sel_label = ui.label(
                    t("{count} selected").format(count=len(state.selected))
                ).classes("text-xs nd-muted")
        with tracks_area:
            for index, track in enumerate(state.tracks):
                _render_track_row(index, track)

    # ------------------------------------------------------------------
    # Wiring and initial render
    # ------------------------------------------------------------------

    platform_sel.on_value_change(_on_platform_change)
    search_btn.on("click", _do_search)
    search_in.on("keydown.enter", _do_search)

    render_note()
    render_login()
    render_playlists()
    render_tracks()

    if state.logged_in:
        _refresh_netease()

    timer = ui.timer(0.3, _drain)

    def _cleanup() -> None:
        state.stopped = True
        state.stop_requested = True
        timer.cancel()

    with suppress(Exception):
        ui.context.client.on_disconnect(_cleanup)
